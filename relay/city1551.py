#!/usr/bin/env python3
"""Ретранслятор открытых данных Киева для AnenjiMon.

Источники:
  * обращения в Контактный центр 1551 (КМДА, data.1551.gov.ua) — API отдаёт только
    целый месяц (до ~100 МБ JSON), без фильтров и сжатия;
  * перечень бюветов (СВКП «Київводфонд», data.gov.ua) — CSV.

Скрипт скачивает текущий месяц, оставляет Київ и нужные категории (вода, тепло, свет,
лифты, газ, бюветы, порывы труб), убирает дубли (одно обращение = строка на каждого
исполнителя) и раскладывает по компактным файлам:

  1551/meta.json            — когда собрано, самая свежая запись источника, счётчики
  1551/index.json           — список улиц → файл
  1551/streets/<slug>.json  — обращения по улице за последние WINDOW_DAYS дней
  1551/areas/<slug>.json    — все улицы массива одним файлом за WINDOW_DAYS (список — relay/areas.json)
  1551/areas/<slug>-recent.json — то же за последние RECENT_HOURS часов (для частого опроса)
  1551/city.json            — по городу: по дням, по часам за 48 ч, «горячие» улицы за 6 ч
  1551/months/ГГГГ-ММ.json.gz — отфильтрованный архив месяца (нужен для окна через границу месяца)
  buvets/buvets.json        — бюветы с координатами, генератором и жалобами «не працює» за 7 дней

Только стандартная библиотека Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    try:
        KYIV = ZoneInfo("Europe/Kyiv")
    except Exception:  # старые базы tzdata
        KYIV = ZoneInfo("Europe/Kiev")
except Exception:  # pragma: no cover
    KYIV = dt.timezone(dt.timedelta(hours=2))

VERSION = 1
API_MONTH = "https://data.1551.gov.ua/api/data/{y:04d}/{m:02d}"
BUVETS_CSV = (
    "https://data.gov.ua/dataset/aa653434-69d3-43a3-b609-5a580d48df16/resource/"
    "d2ff10fc-d217-4371-adcd-e97fa6ba56d5/download/buvets.csv"
)
USER_AGENT = "anenjimon-relay/1 (+https://github.com/rumambo/anenjimon-dtek)"

WINDOW_DAYS = 31      # сколько дней держим в файлах улиц
KEEP_MONTHS = 13      # сколько месячных архивов хранить
HOT_HOURS = 6         # окно «горячих» улиц
HOT_MIN = 3           # минимум обращений на улице за окно, чтобы попасть в «горячие»
HOT_TOP = 30          # сколько «горячих» улиц на категорию
RECENT_HOURS = 48     # окно лёгкого файла массива
REFRESH_PREV_DAYS = 2 # в первые дни месяца перекачиваем прошлый месяц (поздние записи)

# Категории 1551 → короткий код. Для «pipes» берём только подкатегории из PIPES_PREFIXES.
KINDS = {
    "Холодне водопостачання": "cold",
    "Гаряче водопостачання": "hot",
    "Опалення": "heat",
    "Електропостачання": "power",
    "Ліфт": "lift",
    "Газифікація": "gas",
    "Робота бюветів": "buvet",
    "Освітлення приміщень": "light",
    "Обслуговування зовнішніх та внутрішньобудинкових систем": "pipes",
}
PIPES_PREFIXES = (
    "Порив труб у будинку",
    "Витік холодної води на поверхню",
    "Витік каналізації на поверхню",
)
KIND_TITLES = {
    "cold": "Холодна вода",
    "hot": "Гаряча вода",
    "heat": "Опалення",
    "power": "Електропостачання",
    "lift": "Ліфт",
    "gas": "Газ",
    "buvet": "Бювет",
    "light": "Освітлення під'їзду",
    "pipes": "Порив / витік",
}


# ---------------------------------------------------------------- загрузка

def http_get(url: str, timeout: int = 300, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  {url}: попытка {i + 1} не удалась: {e}", file=sys.stderr)
            if i + 1 < attempts:
                time.sleep(10 * (i + 1))
    raise RuntimeError(f"не удалось скачать {url}: {last}")


def load_month_raw(year: int, month: int, source_dir: Path | None) -> list[dict]:
    """Сырые строки 1551 за месяц: из API или из локального файла ГГГГ-ММ.json."""
    if source_dir is not None:
        p = source_dir / f"{year:04d}-{month:02d}.json"
        if not p.exists():
            raise FileNotFoundError(p)
        data = p.read_bytes()
    else:
        url = API_MONTH.format(y=year, m=month)
        print(f"Скачиваю {url}", file=sys.stderr)
        data = http_get(url)
    rows = json.loads(data)
    if not isinstance(rows, list):
        raise ValueError("API вернул не массив")
    return rows


# ---------------------------------------------------------------- обработка

def norm_space(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def kind_code(kind: str | None, content: str | None) -> str | None:
    code = KINDS.get(norm_space(kind))
    if code == "pipes" and not norm_space(content).startswith(PIPES_PREFIXES):
        return None
    return code


def is_kyiv(r: dict) -> bool:
    return norm_space(r.get("addressPostName")) == "Київ" or norm_space(r.get("addressAdminUnitL2")) == "Київ"


def compact(rows: list[dict]) -> list[dict]:
    """Фильтр + дедупликация по uid. Возвращает записи {u,t,k,c,s,h,b}."""
    out: dict[str, dict] = {}
    for r in rows:
        uid = r.get("uid")
        t = r.get("receivedDateTime")
        street = norm_space(r.get("addressThoroughfare"))
        if not uid or not t or not street or not is_kyiv(r):
            continue
        k = kind_code(r.get("kind"), r.get("content"))
        if k is None:
            continue
        rec = {
            "u": uid,
            "t": t,
            "k": k,
            "c": norm_space(r.get("content")),
            "s": street,
            "h": norm_space(r.get("addressLocatorDesignator")) or None,
        }
        b = norm_space(r.get("addressLocatorBuilding"))
        if b and b.lower() != "null":
            rec["b"] = b
        out.setdefault(uid, rec)  # строки одного обращения отличаются только исполнителем
    return sorted(out.values(), key=lambda x: (x["t"], x["u"]))


def parse_t(t: str) -> dt.datetime:
    """'2026-11-04T08:50Z' → aware UTC datetime."""
    t = t.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- транслитерация

_TR = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "y", "і": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ъ": "", "ы": "y", "э": "e", "ё": "io",
}
# первая буква слова / остальные позиции (паспортная транслитерация КМУ 2010)
_TR_POS = {"є": ("ye", "ie"), "ї": ("yi", "i"), "й": ("y", "i"), "ю": ("yu", "iu"), "я": ("ya", "ia")}


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFC", name.lower())
    s = s.replace("’", "").replace("'", "").replace("`", "").replace("ʼ", "")
    out = []
    prev_alpha = False
    for ch in s:
        if ch in _TR_POS:
            out.append(_TR_POS[ch][1 if prev_alpha else 0])
            prev_alpha = True
        elif ch in _TR:
            out.append(_TR[ch])
            prev_alpha = True
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
            prev_alpha = ch.isalpha()
        else:
            out.append("-")
            prev_alpha = False
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "street"


def assign_slugs(names: list[str]) -> dict[str, str]:
    """Стабильные уникальные имена файлов (коллизии получают -2, -3 …)."""
    res: dict[str, str] = {}
    used: Counter[str] = Counter()
    for n in sorted(names):
        base = slugify(n)
        used[base] += 1
        res[n] = base if used[base] == 1 else f"{base}-{used[base]}"
    return res


# ---------------------------------------------------------------- вывод

def write_json(path: Path, obj, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                      separators=None if pretty else (",", ":"), sort_keys=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_month(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(recs, ensure_ascii=False, separators=(",", ":")).encode()
    # mtime=0 — одинаковое содержимое даёт одинаковые байты (git не видит лишних изменений)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, path)


def read_month(path: Path) -> list[dict] | None:
    try:
        with gzip.open(path, "rb") as f:
            data = json.loads(f.read())
        return data if isinstance(data, list) else None
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  {path}: повреждён ({e}), игнорирую", file=sys.stderr)
        return None


def street_item(r: dict) -> dict:
    it = {"t": r["t"], "k": r["k"], "c": r["c"], "h": r["h"]}
    if r.get("b"):
        it["b"] = r["b"]
    return it


def build_streets(out: Path, recs: list[dict]) -> tuple[list[dict], dict[str, str]]:
    by_street: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_street[r["s"]].append(r)
    slugs = assign_slugs(list(by_street))
    sdir = out / "1551" / "streets"
    sdir.mkdir(parents=True, exist_ok=True)
    wanted = set()
    index = []
    for name, items in sorted(by_street.items()):
        slug = slugs[name]
        fname = f"{slug}.json"
        wanted.add(fname)
        items.sort(key=lambda x: (x["t"], x["u"]), reverse=True)
        write_json(sdir / fname, {"street": name, "items": [street_item(x) for x in items]})
        index.append({
            "name": name,
            "file": f"streets/{fname}",
            "n": len(items),
            "last": items[0]["t"],
            "kinds": dict(sorted(Counter(x["k"] for x in items).items())),
        })
    for p in sdir.glob("*.json"):
        if p.name not in wanted:
            p.unlink()
    return index, slugs


def _norm_words(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    s = re.sub(r"[’'`ʼ]", "", s)
    return " " + re.sub(r"[^\w]+", " ", s).strip() + " "


def street_matches(name: str, pattern: str) -> bool:
    """Целые слова pattern встречаются в name подряд (без учёта регистра и апострофов)."""
    return _norm_words(pattern) in _norm_words(name)


def load_areas(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    for slug, a in data.items():
        if not re.fullmatch(r"[a-z0-9-]+", slug) or not a.get("streets"):
            raise ValueError(f"areas.json: неверный массив {slug!r}")
    return data


def build_areas(out: Path, recs: list[dict], areas: dict, slugs: dict[str, str],
                now: dt.datetime) -> dict:
    """1551/areas/<slug>.json — все обращения по улицам массива одним файлом."""
    adir = out / "1551" / "areas"
    adir.mkdir(parents=True, exist_ok=True)
    names = sorted({r["s"] for r in recs})
    summary = {}
    wanted = set()
    for slug, a in sorted(areas.items()):
        matched = {}
        unmatched = []
        for pat in a["streets"]:
            hits = [n for n in names if street_matches(n, pat)]
            if hits:
                for n in hits:
                    matched[n] = pat
            else:
                unmatched.append(pat)
        items = [r for r in recs if r["s"] in matched]
        items.sort(key=lambda x: (x["t"], x["u"]), reverse=True)
        head = {
            "area": slug,
            "title": a.get("title") or slug,
            "streets": [{"name": n, "pattern": matched[n], "file": f"streets/{slugs[n]}.json"}
                        for n in sorted(matched)],
        }
        since = now - dt.timedelta(hours=RECENT_HOURS)
        recent = [x for x in items if parse_t(x["t"]) >= since]
        fname, rname = f"{slug}.json", f"{slug}-recent.json"
        wanted.update((fname, rname))
        write_json(adir / fname, dict(head, items=[dict(street_item(x), s=x["s"]) for x in items]))
        write_json(adir / rname, dict(head, hours=RECENT_HOURS,
                                      items=[dict(street_item(x), s=x["s"]) for x in recent]))
        summary[slug] = {"file": f"areas/{fname}", "recent": f"areas/{rname}",
                         "streets": len(matched), "n": len(items), "n_recent": len(recent)}
        if unmatched:
            summary[slug]["unmatched"] = unmatched
            print(f"  массив {slug}: нет обращений по {unmatched}", file=sys.stderr)
    for p in adir.glob("*.json"):
        if p.name not in wanted:
            p.unlink()
    return summary


def build_city(recs: list[dict], now: dt.datetime) -> dict:
    daily: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        day = parse_t(r["t"]).astimezone(KYIV).strftime("%Y-%m-%d")
        daily[day][r["k"]] += 1

    since48 = now - dt.timedelta(hours=48)
    hourly: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        t = parse_t(r["t"])
        if t >= since48:
            hourly[t.strftime("%Y-%m-%dT%H:00Z")][r["k"]] += 1

    since_hot = now - dt.timedelta(hours=HOT_HOURS)
    hot_cnt: dict[str, Counter] = defaultdict(Counter)
    hot_houses: dict[tuple[str, str], set] = defaultdict(set)
    for r in recs:
        if parse_t(r["t"]) >= since_hot:
            hot_cnt[r["k"]][r["s"]] += 1
            if r["h"]:
                hot_houses[(r["k"], r["s"])].add(r["h"])
    hot = {}
    for k, cnt in hot_cnt.items():
        top = [(s, n) for s, n in cnt.most_common() if n >= HOT_MIN][:HOT_TOP]
        if top:
            hot[k] = [{"street": s, "n": n, "houses": sorted(hot_houses[(k, s)], key=_house_key)}
                      for s, n in top]

    return {
        "kinds": KIND_TITLES,
        "daily": {d: dict(sorted(c.items())) for d, c in sorted(daily.items())},
        "hourly": {h: dict(sorted(c.items())) for h, c in sorted(hourly.items())},
        "hot_hours": HOT_HOURS,
        "hot": dict(sorted(hot.items())),
    }


def _house_key(h: str):
    m = re.match(r"(\d+)", h or "")
    return (int(m.group(1)) if m else 10**9, h)


def house_root(h: str | None) -> str:
    """'8-12' → '8-12', '40-А' → '40-а', ' 7 ' → '7' — для сравнения адресов."""
    return norm_space(h).lower().replace(" ", "")


def build_buvets(out: Path, csv_bytes: bytes | None, recs: list[dict], now: dt.datetime) -> int | None:
    path = out / "buvets" / "buvets.json"
    if csv_bytes is None:
        if not path.exists():
            return None
        prev = json.loads(path.read_text(encoding="utf-8"))
        rows = prev.get("items", [])
        for it in rows:
            it.pop("complaints_7d", None)
        source_updated = prev.get("source_fetched")
    else:
        text = csv_bytes.decode("utf-8-sig")
        rows = []
        for r in csv.DictReader(io.StringIO(text)):
            try:
                lat, lon = float(r.get("lat") or "nan"), float(r.get("lon") or "nan")
            except ValueError:
                continue
            if lat != lat or lon != lon:
                continue
            house = norm_space(r.get("addressLocatorDesignator"))
            rows.append({
                "id": norm_space(r.get("uid")),
                "street": norm_space(r.get("addressThoroughfare")),
                "house": None if house.lower() in ("", "null") else house,
                "district": norm_space(r.get("addressPostDistrict")) or None,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "works": norm_space(r.get("status")).lower() == "працює",
                "generator": norm_space(r.get("generator")).upper() == "TRUE",
                "depth_m": _int_or_none(r.get("wellDepth")),
                "analysis": norm_space(r.get("waterAnalysis")) or None,
            })
        if len(rows) < 50:
            raise ValueError(f"в CSV бюветов подозрительно мало строк: {len(rows)}")
        source_updated = now.strftime("%Y-%m-%dT%H:%MZ")

    since = now - dt.timedelta(days=7)
    complaints: Counter = Counter()
    for r in recs:
        if r["k"] == "buvet" and r["c"].startswith("Не працює") and parse_t(r["t"]) >= since:
            complaints[(r["s"], house_root(r["h"]))] += 1
    for it in rows:
        n = complaints.get((it["street"], house_root(it["house"])), 0)
        if n:
            it["complaints_7d"] = n
    rows.sort(key=lambda x: (x["district"] or "", x["street"], _house_key(x["house"] or "")))
    write_json(path, {
        "source": "data.gov.ua: Бюветні комплекси, розташовані на території міста Києва (СВКП «Київводфонд»)",
        "source_fetched": source_updated,
        "items": rows,
    })
    return len(rows)


def _int_or_none(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- главная

def month_add(y: int, m: int, delta: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + delta
    return i // 12, i % 12 + 1


def run(out: Path, now: dt.datetime, source_dir: Path | None = None,
        buvets_csv: bytes | None | str = "fetch", areas: dict | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    mdir = out / "1551" / "months"
    cur = (now.year, now.month)          # месяцы в API — по времени записей (UTC-метки)
    prev = month_add(*cur, -1)

    old_meta = {}
    try:
        old_meta = json.loads((out / "1551" / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    # текущий месяц — всегда
    cur_recs = compact(load_month_raw(*cur, source_dir))
    old_cnt = (old_meta.get("months") or {}).get(f"{cur[0]:04d}-{cur[1]:02d}")
    if old_cnt and len(cur_recs) < old_cnt * 0.5:
        raise RuntimeError(f"в текущем месяце {len(cur_recs)} записей, было {old_cnt} — источник сломан?")
    if not cur_recs and now.day > 1:
        raise RuntimeError("в текущем месяце нет ни одной записи — источник сломан?")
    write_month(mdir / f"{cur[0]:04d}-{cur[1]:02d}.json.gz", cur_recs)

    # прошлый месяц — из архива; в первые дни месяца или если архива нет — перекачать
    prev_path = mdir / f"{prev[0]:04d}-{prev[1]:02d}.json.gz"
    prev_recs = read_month(prev_path)
    if prev_recs is None or now.day <= REFRESH_PREV_DAYS:
        try:
            fresh = compact(load_month_raw(*prev, source_dir))
            if prev_recs is None or len(fresh) >= len(prev_recs) * 0.9:
                prev_recs = fresh
                write_month(prev_path, prev_recs)
        except Exception as e:  # noqa: BLE001
            print(f"  прошлый месяц недоступен: {e}", file=sys.stderr)
            if prev_recs is None:
                prev_recs = []

    # старые архивы
    keep = {f"{y:04d}-{m:02d}.json.gz" for y, m in (month_add(*cur, -i) for i in range(KEEP_MONTHS))}
    for p in mdir.glob("*.json.gz"):
        if p.name not in keep:
            p.unlink()

    since = now - dt.timedelta(days=WINDOW_DAYS)
    seen = set()
    window = []
    for r in prev_recs + cur_recs:
        if r["u"] in seen:
            continue
        seen.add(r["u"])
        if parse_t(r["t"]) >= since:
            window.append(r)

    index, slugs = build_streets(out, window)
    write_json(out / "1551" / "index.json", {"streets": index})
    area_summary = build_areas(out, window, areas or {}, slugs, now)
    write_json(out / "1551" / "city.json", build_city(window, now))

    if buvets_csv == "fetch":
        try:
            buvets_csv = http_get(BUVETS_CSV, timeout=60)
        except Exception as e:  # noqa: BLE001
            print(f"  бюветы недоступны, оставляю прошлый список: {e}", file=sys.stderr)
            buvets_csv = None
    n_buvets = build_buvets(out, buvets_csv, window, now)

    latest = max((r["t"] for r in cur_recs), default=None) or max((r["t"] for r in prev_recs), default=None)
    months = {}
    for p in sorted(mdir.glob("*.json.gz")):
        recs = read_month(p)
        months[p.name[:7]] = len(recs) if recs is not None else None
    meta = {
        "version": VERSION,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_latest": latest,
        "lag_minutes": round((now - parse_t(latest)).total_seconds() / 60) if latest else None,
        "window_days": WINDOW_DAYS,
        "window_records": len(window),
        "streets": len(index),
        "kinds": dict(sorted(Counter(r["k"] for r in window).items())),
        "months": months,
        "areas": area_summary,
        "buvets": n_buvets,
        "source": "КБУ «Контактний центр міста Києва» (1551), data.gov.ua, CC BY 4.0",
    }
    write_json(out / "1551" / "meta.json", meta, pretty=True)
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="каталог с опубликованными данными (ветка data)")
    ap.add_argument("--source-dir", type=Path, help="брать месяцы 1551 из файлов ГГГГ-ММ.json вместо API")
    ap.add_argument("--buvets-file", type=Path, help="брать CSV бюветов из файла")
    ap.add_argument("--areas", type=Path, default=Path(__file__).with_name("areas.json"),
                    help="массивы: {slug: {title, streets: [часть названия улицы]}}")
    ap.add_argument("--now", help="текущее время UTC, ISO 8601 (для тестов)")
    a = ap.parse_args(argv)
    now = parse_t(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    buv = a.buvets_file.read_bytes() if a.buvets_file else "fetch"
    meta = run(a.out, now, a.source_dir, buv, load_areas(a.areas))
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
