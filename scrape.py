"""İÜC SKS yemek listesini çekip menu.json'a yazar.

- Sadece "Öğle Yemeği" sekmesini alır (akşam ile aynı).
- Sayfa açılmazsa 3 kereye kadar tekrar dener.
- Hiç gün bulamazsa hata verir ve mevcut menu.json'u ezmez.
- Eski günleri korur, yeni günleri ekler/günceller.
"""
import json
import pathlib
import re
import sys
import time
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

URL = "https://sks.iuc.edu.tr/tr/yemeklistesi"
OUT = pathlib.Path("menu.json")
WIDGET_OUT = pathlib.Path("widget.json")  # KWGT için sade, düz yapı
BLANK = "\u00a0"  # KWGT boş ("") değeri null sayıp hata veriyor; görünmez boşluk kullan
ATTEMPTS = 3
WAIT_BETWEEN = 15  # saniye

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


def build_widget(menu):
    """menu.json verisinden KWGT'nin kolay okuyacağı düz bir yapı üretir.

    Anahtar: d20261006 (harfle başlar, tiresiz).
    Her gün için l1..l5 (satırlar), kcal ve title bulunur.
    Aradaki günler (hafta sonu vb.) için hazır mesaj yazılır.
    """
    if not menu:
        return {}
    dates = sorted(datetime.strptime(k, "%Y-%m-%d").date() for k in menu)
    start = dates[0]
    last = dates[-1]
    # son verinin ayının sonuna kadar doldur
    nxt = (last.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = nxt - timedelta(days=1)

    out = {}
    d = start
    while d <= end:
        iso = d.strftime("%Y-%m-%d")
        key = "d" + d.strftime("%Y%m%d")
        if iso in menu:
            lines = list(menu[iso]["dishes"])
            kcal = menu[iso].get("kcal")
            kcal_txt = f"{kcal} kcal" if kcal else BLANK
        elif d.weekday() >= 5:
            lines, kcal_txt = ["Hafta sonu yemek yok :("], BLANK
        else:
            lines, kcal_txt = ["Bugün menü bulunamadı"], BLANK
        lines = (lines + [BLANK] * 5)[:5]
        out[key] = {
            "title": "GÜNÜN MENÜSÜ",
            "l1": lines[0], "l2": lines[1], "l3": lines[2],
            "l4": lines[3], "l5": lines[4],
            "kcal": kcal_txt,
            "all": "\n".join(x for x in lines if x.strip()),
        }
        d += timedelta(days=1)
    return out


def write_day_files(widget):
    """Her gün için KWGT'nin düz metin (txt) olarak okuyacağı dosyalar yazar.

    KWGT satır sonlarını boşluğa çevirdiği için her satır AYRI dosyadır:
    days/20261006.1.txt ... days/20261006.5.txt  -> yemek satırları (boşsa görünmez boşluk)
    days/20261006.kcal.txt                        -> kalori (yoksa görünmez boşluk)
    """
    folder = pathlib.Path("days")
    folder.mkdir(exist_ok=True)
    for key, v in widget.items():
        day = key[1:]  # "d20261006" -> "20261006"
        for i in range(1, 6):
            (folder / f"{day}.{i}.txt").write_text(v[f"l{i}"] or BLANK, encoding="utf-8")
        (folder / f"{day}.kcal.txt").write_text(v["kcal"] or BLANK, encoding="utf-8")


def parse(raw):
    days = {}
    for item in raw:
        iso = datetime.strptime(item["date"], "%d.%m.%Y").strftime("%Y-%m-%d")
        dishes, kcal = [], None
        for line in item["lines"]:
            if line == item["date"] or re.fullmatch(r"\s*kalori\s*", line, re.I):
                continue
            m = re.search(r"(\d+)\s*kalori", line, re.I)
            if m:
                kcal = int(m.group(1))
            else:
                dishes.append(line)
        if dishes:
            days[iso] = {"dishes": dishes, "kcal": kcal}
    return days


def fetch_once():
    """Sayfayı bir kez açıp ham kart verisini döndürür."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            # networkidle yerine domcontentloaded: arka plan istekleri takılma yapmasın
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)

            # Angular ile sonradan yüklenir: DOM'a gelmesini bekle (görünür olmasını değil)
            page.wait_for_selector(
                "text=/\\d{2}\\.\\d{2}\\.\\d{4}/", state="attached", timeout=45000
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
                print(
                    f"DEBUG: toplam tarih öğesi={info['total']}, görünür={info['visible']}"
                )
                print("DEBUG sayfa metni (ilk 1500 karakter):")
                print(info["body"])
            return raw
        finally:
            browser.close()


def main():
    days = {}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            days = parse(fetch_once())
            if days:
                break
            print(f"Deneme {attempt}/{ATTEMPTS}: veri bulunamadı.")
        except Exception as e:
            print(f"Deneme {attempt}/{ATTEMPTS} başarısız: {type(e).__name__}: {str(e)[:200]}")
        if attempt < ATTEMPTS:
            time.sleep(WAIT_BETWEEN)

    if not days:
        print("HATA: hiç gün bulunamadı, menu.json değiştirilmedi.", file=sys.stderr)
        sys.exit(1)

    existing = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    existing.update(days)
    OUT.write_text(
        json.dumps(existing, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    widget = build_widget(existing)
    WIDGET_OUT.write_text(
        json.dumps(widget, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    write_day_files(widget)
    print(f"{len(days)} gün işlendi, toplam {len(existing)} gün kayıtlı.")
    print(f"widget.json: {len(widget)} gün (hafta sonları dahil).")


if __name__ == "__main__":
    main()
