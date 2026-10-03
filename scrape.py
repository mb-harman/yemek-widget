"""İÜC SKS yemek listesini çekip menu.json'a yazar.

- Sadece "Öğle Yemeği" sekmesini alır (akşam ile aynı).
- Hiç gün bulamazsa hata verir ve mevcut menu.json'u ezmez.
- Eski günleri korur, yeni günleri ekler/günceller.
"""
import json
import pathlib
import re
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

URL = "https://sks.iuc.edu.tr/tr/yemeklistesi"
OUT = pathlib.Path("menu.json")

# Sayfadaki her tarih kartını bulur (sadece GÖRÜNÜR olanlar):
# içinde SADECE bir tarih geçen en büyük kapsayıcıyı kart sayar.
EXTRACT_JS = r"""
() => {
  const dateRe = /^\d{2}\.\d{2}\.\d{4}$/;
  const anyDate = /\d{2}\.\d{2}\.\d{4}/g;
  const results = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('*')) {
    if (el.children.length) continue;
    if (el.offsetParent === null) continue;   // gizli öğeleri atla
    const t = el.textContent.trim();
    if (!dateRe.test(t) || seen.has(t)) continue;
    let card = el;
    while (card.parentElement) {
      const p = card.parentElement;
      const dates = (p.innerText || '').match(anyDate) || [];
      if (dates.length > 1) break;
      card = p;
    }
    seen.add(t);
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    results.push({ date: t, lines });
  }
  return results;
}
"""

DEBUG_JS = r"""
() => {
  const re = /^\d{2}\.\d{2}\.\d{4}$/;
  let total = 0, visible = 0;
  for (const el of document.querySelectorAll('*')) {
    if (el.children.length) continue;
    if (!re.test(el.textContent.trim())) continue;
    total++;
    if (el.offsetParent !== null) visible++;
  }
  return { total, visible, body: (document.body.innerText || '').slice(0, 1500) };
}
"""


def parse(raw):
    days = {}
    for item in raw:
        iso = datetime.strptime(item["date"], "%d.%m.%Y").strftime("%Y-%m-%d")
        dishes, kcal = [], None
        for line in item["lines"]:
            if line == item["date"]:
                continue
            m = re.search(r"(\d+)\s*kalori", line, re.I)
            if m:
                kcal = int(m.group(1))
            else:
                dishes.append(line)
        if dishes:
            days[iso] = {"dishes": dishes, "kcal": kcal}
    return days


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Angular ile sonradan yüklenir: DOM'a gelmesini bekle (görünür olmasını değil)
        page.wait_for_selector(
            "text=/\\d{2}\\.\\d{2}\\.\\d{4}/", state="attached", timeout=30000
        )

        # Öğle sekmesine tıkla (zaten açıksa sorun değil)
        try:
            page.get_by_text("Öğle Yemeği").locator("visible=true").first.click(
                timeout=5000
            )
        except Exception as e:
            print(f"Sekme tıklanamadı (devam ediliyor): {type(e).__name__}")
        page.wait_for_timeout(2000)

        raw = page.evaluate(EXTRACT_JS)
        if not raw:
            info = page.evaluate(DEBUG_JS)
            print(f"DEBUG: toplam tarih öğesi={info['total']}, görünür={info['visible']}")
            print("DEBUG sayfa metni (ilk 1500 karakter):")
            print(info["body"])
        browser.close()

    days = parse(raw)
    if not days:
        print("HATA: hiç gün bulunamadı, menu.json değiştirilmedi.", file=sys.stderr)
        sys.exit(1)

    existing = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    existing.update(days)
    OUT.write_text(
        json.dumps(existing, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    print(f"{len(days)} gün işlendi, toplam {len(existing)} gün kayıtlı.")


if __name__ == "__main__":
    main()
