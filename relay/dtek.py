#!/usr/bin/env python3
"""Ретранслятор даних про аварії ДТЕК для AnenjiMon (масив Північно-Броварський).

Запускається через GitHub Actions кожні 10 хвилин. Відкриває безголовий браузер
(Playwright Chromium) на сторінці https://www.dtek-kem.com.ua/ua/shutdowns,
проходить WAF (Cloudflare / Incapsula) та у сесії сторінки опитує внутрішній
AJAX ендпоінт ДТЕК (/ua/ajax) за 8 вулицями масиву:

  * вул. Малишка Андрія
  * вул. Солов’яненка Анатолія (Бойченка Олександра)
  * вул. Миропільська
  * бул. Дарницький
  * вул. Шалетт міста
  * вул. Юності
  * вул. Космічна
  * вул. Князя Романа Мстиславича (Генерала Жмаченка)

Результати записуються у директорію out/ (для гілки `data`):
  dtek/latest.json                     — повні дані по всіх 8 вулицях та будинках
  dtek/active_emergencies.json         — список тільки активних аварійних відключень
  dtek/status.json                     — компактний статус для легких віджетів
  dtek/areas/pivnichno-brovarskyi.json — зріз аварій по масиву Північно-Броварський
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))

# Імпортуємо правила нормалізації та зіставлення вулиць із city1551
try:
    from relay.city1551 import load_areas, street_matches, write_json
except ImportError:
    from city1551 import load_areas, street_matches, write_json

DTEK_URL = "https://www.dtek-kem.com.ua/ua/shutdowns"


def get_kyiv_time(now: dt.datetime | None = None) -> tuple[str, str]:
    """Повертає ISO UTC час та читабельний рядок за київським часом."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    # Kyiv: UTC+2 winter / UTC+3 summer
    # Використовуємо zoneinfo, якщо доступно, інакше зміщення
    try:
        import zoneinfo
        tz_kyiv = zoneinfo.ZoneInfo("Europe/Kyiv")
        kyiv_dt = now.astimezone(tz_kyiv)
    except Exception:
        # Резервний розрахунок для Києва (EET/EEST)
        kyiv_dt = now + dt.timedelta(hours=2)
    
    iso_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    kyiv_str = kyiv_dt.strftime("%d.%m.%Y, %H:%M:%S")
    return iso_utc, kyiv_str


def scrape_dtek(target_streets: list[str]) -> dict:
    """Запускає Playwright і збирає дані про аварії з сайту ДТЕК."""
    from playwright.sync_api import sync_playwright

    print(f"Запуск Chromium через Playwright для {DTEK_URL}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
        )

        page = context.new_page()
        page.on("console", lambda msg: print(f"[Browser] {msg.text}"))

        max_attempts = 3
        scrape_result = None

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Спроба {attempt}: завантаження сторінки...")
                resp = page.goto(DTEK_URL, wait_until="domcontentloaded", timeout=45000)
                status = resp.status if resp else None
                print(f"Статус відповіді: {status}")

                # Очікування проходження WAF (Cloudflare/Incapsula)
                page.wait_for_timeout(6000)

                scrape_result = page.evaluate(
                    """async (streetsToMonitor) => {
                        function norm(s) {
                            if (!s) return '';
                            return s.toLowerCase()
                                .replace(/['’`ʼ]/g, "'")
                                .replace(/вул\\.|вулиця|бульв\\.|бульвар|просп\\.|проспект|пров\\.|провулок/g, '')
                                .replace(/\\s+/g, ' ')
                                .trim();
                        }

                        // 1. Пошук адреси AJAX-обробника та Yii2 CSRF токена
                        let metaAjax = document.querySelector('meta[name="ajaxUrl"]')?.getAttribute('content');
                        let ajaxUrl = metaAjax || window.ajaxUrl;
                        if (!ajaxUrl) {
                            for (const s of document.querySelectorAll('script')) {
                                const t = s.innerText || '';
                                const m = t.match(/ajax(?:_url|Url)?\s*[:=]\s*['"]([^'"]+)['"]/i);
                                if (m) { ajaxUrl = m[1]; break; }
                            }
                        }
                        if (!ajaxUrl) ajaxUrl = '/ua/ajax';
                        console.log('[DTEK] Discovered ajaxUrl:', ajaxUrl);

                        // Знаходимо CSRF параметри Yii2
                        const csrfParam = document.querySelector('meta[name="csrf-param"]')?.getAttribute('content') ||
                                          (window.yii && window.yii.getCsrfParam ? window.yii.getCsrfParam() : '_csrf');
                        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ||
                                          (window.yii && window.yii.getCsrfToken ? window.yii.getCsrfToken() : null);
                        console.log('[DTEK] CSRF Param:', csrfParam, 'Token present:', !!csrfToken);

                        const ajaxHeaders = {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'
                        };
                        if (csrfToken) {
                            ajaxHeaders['X-CSRF-Token'] = csrfToken;
                        }

                        // 2. Оголошення або глобальний банер про екстрені відключення (тільки видимі на екрані)
                        const alertEls = Array.from(document.querySelectorAll('.alert, .discon-alert, .emergency-notice, [class*="alert"]'))
                            .filter(el => el.offsetParent !== null && window.getComputedStyle(el).display !== 'none');
                        const bannerText = alertEls.map(el => el.innerText.trim()).filter(Boolean).join('\\n') || null;
                        const isGlobalEmergency = bannerText ? /аварі|екстрен/i.test(bannerText) : false;

                        // 3. Запит списку доступних вулиць
                        let allStreets = [];
                        let city = 'м. Київ';

                        try {
                            const f = new URLSearchParams();
                            if (csrfParam && csrfToken) {
                                f.append(csrfParam, csrfToken);
                            }
                            f.append('method', 'getStreets');
                            f.append('action', 'getStreets');
                            const res = await fetch(ajaxUrl, {
                                method: 'POST',
                                headers: ajaxHeaders,
                                body: f.toString()
                            });
                            console.log('[DTEK] getStreets status:', res.status);
                            const textStreets = await res.text();
                            console.log('[DTEK] getStreets raw length:', textStreets.length, 'sample:', textStreets.substring(0, 150));
                            if (res.ok && textStreets) {
                                const json = JSON.parse(textStreets);
                                if (json && json.streets) {
                                    if (Array.isArray(json.streets)) {
                                        allStreets = json.streets;
                                    } else if (typeof json.streets === 'object') {
                                        for (const c of Object.keys(json.streets)) {
                                            if (/київ/i.test(c)) city = c;
                                            const l = json.streets[c];
                                            if (Array.isArray(l)) allStreets.push(...l);
                                            else if (typeof l === 'object' && l !== null) {
                                                allStreets.push(...Object.keys(l), ...Object.values(l).filter(v => typeof v === 'string'));
                                            }
                                        }
                                    }
                                }
                            }
                        } catch (e) {
                            console.error('[DTEK] getStreets error:', e);
                        }

                        // Якщо через AJAX не отримали, шукаємо у випадаючих списках сторінки
                        if (allStreets.length === 0) {
                            const domOptions = Array.from(document.querySelectorAll('select option, datalist option')).map(o => (o.value || o.innerText).trim()).filter(Boolean);
                            if (domOptions.length > 0) {
                                console.log('[DTEK] Found streets in DOM:', domOptions.length);
                                allStreets = domOptions;
                            }
                        }

                        allStreets = Array.from(new Set(allStreets));
                        console.log('[DTEK] Total unique streets found:', allStreets.length);

                        // 4. Опитування будинків для кожної вулиці
                        const streetResults = {};
                        const activeEmergencies = [];

                        for (const targetPattern of streetsToMonitor) {
                            // Знаходимо назву вулиці в базі ДТЕК
                            let matchedName = allStreets.find(s => {
                                const sNorm = norm(s);
                                const pNorm = norm(targetPattern);
                                const pWords = pNorm.split(' ').filter(w => w.length > 2);
                                return pWords.every(w => sNorm.includes(w));
                            });

                            if (!matchedName) {
                                matchedName = targetPattern;
                            }
                            console.log(`[DTEK] Target: "${targetPattern}" -> Matched: "${matchedName}"`);

                            const timestamp = Math.floor(Date.now() / 1000);
                            let homeData = null;

                            // Спроба 1: WordPress / Yii2 serializedArray
                            try {
                                const p1 = new URLSearchParams();
                                if (csrfParam && csrfToken) {
                                    p1.append(csrfParam, csrfToken);
                                }
                                p1.append('method', 'getHomeNum');
                                p1.append('action', 'getHomeNum');
                                p1.append('data[0][name]', 'city');
                                p1.append('data[0][value]', city);
                                p1.append('data[1][name]', 'street');
                                p1.append('data[1][value]', matchedName);
                                p1.append('data[2][name]', 'updateFact');
                                p1.append('data[2][value]', String(timestamp));

                                const r1 = await fetch(ajaxUrl, {
                                    method: 'POST',
                                    headers: ajaxHeaders,
                                    body: p1.toString()
                                });
                                const t1 = await r1.text();
                                if (r1.ok && t1) {
                                    const j1 = JSON.parse(t1);
                                    if (j1 && j1.data) {
                                        homeData = j1;
                                        console.log(`[DTEK] Homes found for "${matchedName}" via p1:`, Object.keys(j1.data).length);
                                    }
                                }
                            } catch (e) {
                                console.error(`[DTEK] getHomeNum p1 error for ${matchedName}:`, e);
                            }

                            // Спроба 2: прямі параметри
                            if (!homeData || !homeData.data) {
                                try {
                                    const p2 = new URLSearchParams();
                                    if (csrfParam && csrfToken) {
                                        p2.append(csrfParam, csrfToken);
                                    }
                                    p2.append('method', 'getHomeNum');
                                    p2.append('action', 'getHomeNum');
                                    p2.append('city', city);
                                    p2.append('street', matchedName);
                                    p2.append('updateFact', String(timestamp));

                                    const r2 = await fetch(ajaxUrl, {
                                        method: 'POST',
                                        headers: ajaxHeaders,
                                        body: p2.toString()
                                    });
                                    const t2 = await r2.text();
                                    if (r2.ok && t2) {
                                        const j2 = JSON.parse(t2);
                                        if (j2 && j2.data) {
                                            homeData = j2;
                                            console.log(`[DTEK] Homes found for "${matchedName}" via p2:`, Object.keys(j2.data).length);
                                        }
                                    }
                                } catch (e) {
                                    console.error(`[DTEK] getHomeNum p2 error for ${matchedName}:`, e);
                                }
                            }

                            const streetEmergencies = [];
                            const housesInfo = {};
                            let housesCount = 0;

                            if (homeData && homeData.data) {
                                const raw = homeData.data;
                                housesCount = Object.keys(raw).length;

                                for (const [houseNo, info] of Object.entries(raw)) {
                                    const subType = info?.sub_type || '';
                                    const reasons = Array.isArray(info?.sub_type_reason)
                                        ? info.sub_type_reason
                                        : (info?.sub_type_reason ? [String(info.sub_type_reason)] : []);

                                    // Виявлення аварії / пошкодження
                                    const isEm = /аварі|екстрен|пошкодж|несправн|локальн/i.test(subType) ||
                                                 reasons.some(r => /аварі|екстрен|пошкодж|несправн|локальн/i.test(r));

                                    let outageType = 'ok';
                                    if (isEm) {
                                        outageType = 'emergency';
                                    } else if (/стабілізац/i.test(subType) || reasons.some(r => /gpv|стабілізац/i.test(r))) {
                                        outageType = 'stabilization';
                                    } else if (/планов/i.test(subType)) {
                                        outageType = 'planned';
                                    }

                                    housesInfo[houseNo] = {
                                        house: houseNo,
                                        outage_type: outageType,
                                        is_emergency: isEm,
                                        sub_type: subType,
                                        reasons: reasons,
                                        start_date: info?.start_date || null,
                                        end_date: info?.end_date || null
                                    };

                                    if (isEm) {
                                        const record = {
                                            pattern: targetPattern,
                                            matched_street: matchedName,
                                            house: houseNo,
                                            sub_type: subType,
                                            reasons: reasons,
                                            start_date: info?.start_date || null,
                                            end_date: info?.end_date || null
                                        };
                                        streetEmergencies.push(record);
                                        activeEmergencies.push(record);
                                    }
                                }
                            }

                            streetResults[targetPattern] = {
                                pattern: targetPattern,
                                matched_name: matchedName,
                                has_emergency: streetEmergencies.length > 0,
                                emergencies_count: streetEmergencies.length,
                                emergencies: streetEmergencies,
                                houses_total: housesCount,
                                houses: housesInfo,
                                preset: homeData?.preset || null,
                                fact: homeData?.fact || null
                            };
                        }

                        return {
                            banner: bannerText,
                            is_global_emergency: isGlobalEmergency,
                            streets_found_total: allStreets.length,
                            active_emergencies: activeEmergencies,
                            streets: streetResults
                        };
                    }""",
                    target_streets,
                )

                if scrape_result:
                    print("Дані ДТЕК успішно зібрано.")
                    break
            except Exception as err:
                print(f"Помилка спроби {attempt}: {err}")
                if attempt < max_attempts:
                    page.wait_for_timeout(8000)
                else:
                    browser.close()
                    raise

        browser.close()

    if not scrape_result:
        raise RuntimeError("Не вдалося зібрати дані з ДТЕК після кількох спроб.")

    return scrape_result


def build_markdown_dashboard(kyiv_str: str, streets_data: dict, active_emergencies: list[dict]) -> str:
    """Генерує зручну та зрозумілу таблицю в форматі Markdown для нетехнічних користувачів."""
    lines = [
        "# ⚡ Стан електропостачання та аварії ДТЕК",
        "",
        f"> **Останнє оновлення:** {kyiv_str} *(оновлюється кожні 10 хвилин)*  ",
    ]

    if active_emergencies:
        lines.append(f"> **Загальний стан:** 🔴 **ВИЯВЛЕНО АВАРІЇ ({len(active_emergencies)})**")
    else:
        lines.append("> **Загальний стан:** 🟢 **ШТАТНИЙ РЕЖИМ (АВАРІЙ НЕМАЄ)**")

    lines.extend([
        "",
        "---",
        "",
        "### 🚨 Де зараз аварії та коли повернуть світло",
        "",
    ])

    if active_emergencies:
        lines.append("| 📍 Адреса | Статус | Причина | Коли зникло | 💡 Орієнтовне включення |")
        lines.append("|---|:---:|---|:---:|:---:|")
        for em in active_emergencies:
            street = em.get("matched_street") or em.get("pattern") or ""
            house = em.get("house") or ""
            sub_type = em.get("sub_type") or "Аварійний ремонт"
            start_date = em.get("start_date") or "—"
            end_date = em.get("end_date") or "Уточнюється"
            lines.append(f"| **{street}, {house}** | 🔴 **Немає світла** | {sub_type} | {start_date} | **{end_date}** |")
    else:
        lines.append("🟢 **Наразі активних аварійних відключень не зафіксовано.**  ")
        lines.append("Електропостачання здійснюється у звичайному режимі або за графіками стабілізаційних відключень.")

    lines.extend([
        "",
        "---",
        "",
        "### 📋 Загальна ситуація по всіх 8 вулицях масиву",
        "",
        "| Вулиця | Всього будинків | Поточний стан |",
        "|---|:---:|---|",
    ])

    aliases = {
        "вул. Князя Романа Мстиславича": " *(кол. Жмаченка)*",
        "вул. Солов’яненка Анатолія": " *(кол. Бойченка)*",
        "вул. Шалетт міста": " *(Міста Шалетт)*",
    }

    for pattern, d in streets_data.items():
        matched = d.get("matched_name") or pattern
        extra = aliases.get(pattern, "")
        houses_total = d.get("houses_total", 0)
        has_em = d.get("has_emergency", False)
        em_list = d.get("emergencies", [])

        if has_em and em_list:
            houses_str = ", ".join(sorted([str(e.get("house", "")) for e in em_list], key=lambda x: (int(re.sub(r'\D', '', x) or 0), x)))
            other_count = max(0, houses_total - len(em_list))
            other_text = f" *(в інших {other_count} будинках аварій немає)*" if other_count > 0 else ""
            status_desc = f"⚠️ **Аварія в будинках № {houses_str}**{other_text}"
        else:
            status_desc = "🟢 **Аварій немає**, електропостачання в нормі"

        lines.append(f"| **{matched}**{extra} | {houses_total} | {status_desc} |")

    lines.extend([
        "",
        "---",
        "",
        "> ℹ️ *Дані автоматично збираються безпосередньо з сайту ДТЕК Київські електромережі за допомогою GitHub Actions раз на 10 хвилин.*",
        "",
    ])
    return "\n".join(lines)


def build_html_dashboard(kyiv_str: str, streets_data: dict, active_emergencies: list[dict], target_streets: list[str]) -> str:
    """Генерує зручну, сучасну та адаптивну HTML-сторінку зі статусами та таблицями."""
    status_text = f"ВИЯВЛЕНО АВАРІЇ ({len(active_emergencies)})" if active_emergencies else "ШТАТНИЙ РЕЖИМ (АВАРІЙ НЕМАЄ)"
    status_badge_bg = "#dc2626" if active_emergencies else "#16a34a"
    status_icon = "🔴" if active_emergencies else "🟢"
    total_houses = sum(d.get("houses_total", 0) for d in streets_data.values())

    if active_emergencies:
        em_rows = []
        for em in active_emergencies:
            street = em.get("matched_street") or em.get("pattern") or ""
            house = em.get("house") or ""
            sub_type = em.get("sub_type") or "Аварійний ремонт"
            start_date = em.get("start_date") or "—"
            end_date = em.get("end_date") or "Уточнюється"
            reasons = "; ".join(em.get("reasons", []))
            em_rows.append(f"""
            <tr>
              <td><strong>{street}, {house}</strong></td>
              <td><span class="badge badge-red">🔴 Немає світла</span></td>
              <td>{sub_type} <span class="sub-text">({reasons})</span></td>
              <td>{start_date}</td>
              <td class="highlight">{end_date}</td>
            </tr>""")
        emergencies_section = f"""
        <div class="card">
          <h2>🚨 Де зараз аварії та коли повернуть світло</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>📍 Адреса</th>
                  <th>Статус</th>
                  <th>Причина</th>
                  <th>Коли зникло</th>
                  <th>💡 Орієнтовне включення</th>
                </tr>
              </thead>
              <tbody>
                {''.join(em_rows)}
              </tbody>
            </table>
          </div>
        </div>"""
    else:
        emergencies_section = """
        <div class="card">
          <h2>🚨 Де зараз аварії</h2>
          <div class="ok-box">
            <span class="ok-icon">🟢</span>
            <div>
              <strong>Наразі активних аварійних відключень не зафіксовано.</strong>
              <p>Електропостачання здійснюється у звичайному режимі або за графіками стабілізаційних відключень.</p>
            </div>
          </div>
        </div>"""

    street_rows = []
    aliases = {
        "вул. Князя Романа Мстиславича": " <span class='sub-text'>(кол. Жмаченка)</span>",
        "вул. Солов’яненка Анатолія": " <span class='sub-text'>(кол. Бойченка)</span>",
        "вул. Шалетт міста": " <span class='sub-text'>(Міста Шалетт)</span>",
    }
    for pattern, d in streets_data.items():
        matched = d.get("matched_name") or pattern
        extra = aliases.get(pattern, "")
        houses_cnt = d.get("houses_total", 0)
        has_em = d.get("has_emergency", False)
        em_list = d.get("emergencies", [])
        if has_em and em_list:
            houses_str = ", ".join(sorted([str(e.get("house", "")) for e in em_list], key=lambda x: (int(re.sub(r'\D', '', x) or 0), x)))
            badge = '<span class="badge badge-red">⚠️ Аварія</span>'
            status_desc = f"<strong>Аварія в будинках № {houses_str}</strong>"
        else:
            badge = '<span class="badge badge-green">🟢 Норма</span>'
            status_desc = "Аварій немає, світло є"

        street_rows.append(f"""
        <tr>
          <td><strong>{matched}</strong>{extra}</td>
          <td style="text-align: center;">{houses_cnt}</td>
          <td style="text-align: center;">{badge}</td>
          <td>{status_desc}</td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>⚡ Стан електропостачання та аварії ДТЕК — Північно-Броварський масив</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.15);
      --green: #22c55e;
      --green-bg: rgba(34, 197, 94, 0.15);
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f8fafc;
        --card-bg: #ffffff;
        --card-border: #e2e8f0;
        --text: #0f172a;
        --text-muted: #64748b;
        --accent: #0284c7;
        --red: #dc2626;
        --red-bg: #fee2e2;
        --green: #16a34a;
        --green-bg: #dcfce7;
      }}
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 1.5rem 1rem;
    }}
    .container {{
      max-width: 900px;
      margin: 0 auto;
    }}
    header {{
      margin-bottom: 2rem;
      text-align: center;
    }}
    h1 {{
      font-size: 1.6rem;
      margin-bottom: 0.5rem;
      font-weight: 700;
    }}
    .meta-bar {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
      justify-content: center;
      font-size: 0.9rem;
      color: var(--text-muted);
      margin-top: 0.25rem;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-weight: 600;
      font-size: 0.85rem;
      color: white;
      background: {status_badge_bg};
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }}
    .stat-box {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1rem;
      text-align: center;
    }}
    .stat-val {{
      font-size: 1.8rem;
      font-weight: 700;
      color: var(--accent);
    }}
    .stat-val.red {{ color: var(--red); }}
    .stat-val.green {{ color: var(--green); }}
    .stat-label {{
      font-size: 0.85rem;
      color: var(--text-muted);
      margin-top: 0.25rem;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 1.25rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    }}
    h2 {{
      font-size: 1.2rem;
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }}
    th, td {{
      padding: 0.75rem 0.85rem;
      text-align: left;
      border-bottom: 1px solid var(--card-border);
    }}
    th {{
      font-weight: 600;
      color: var(--text-muted);
      background: rgba(0,0,0,0.02);
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .badge {{
      display: inline-block;
      padding: 0.2rem 0.6rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    .badge-red {{ background: var(--red-bg); color: var(--red); }}
    .badge-green {{ background: var(--green-bg); color: var(--green); }}
    .highlight {{ font-weight: 700; color: var(--red); }}
    .sub-text {{ font-size: 0.8rem; color: var(--text-muted); font-weight: normal; }}
    .ok-box {{
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 1rem;
      background: var(--green-bg);
      border-radius: 10px;
      color: var(--green);
    }}
    .ok-icon {{ font-size: 2rem; }}
    .ok-box p {{ color: var(--text); font-size: 0.9rem; margin-top: 0.25rem; }}
    footer {{
      text-align: center;
      font-size: 0.85rem;
      color: var(--text-muted);
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid var(--card-border);
    }}
    footer a {{ color: var(--accent); text-decoration: none; }}
    footer a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>⚡ Стан електропостачання та аварії ДТЕК</h1>
      <p style="color: var(--text-muted);">Північно-Броварський масив (м. Київ, Дніпровський район)</p>
      <div class="meta-bar">
        <span>🕒 Оновлено: <strong>{kyiv_str}</strong></span>
        <span>•</span>
        <span class="status-pill">{status_icon} {status_text}</span>
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-val">{len(target_streets)}</div>
        <div class="stat-label">Вулиць на моніторингу</div>
      </div>
      <div class="stat-box">
        <div class="stat-val">{total_houses}</div>
        <div class="stat-label">Будинків перевірено</div>
      </div>
      <div class="stat-box">
        <div class="stat-val {'red' if active_emergencies else 'green'}">{len(active_emergencies)}</div>
        <div class="stat-label">Активних аварій</div>
      </div>
    </div>

    {emergencies_section}

    <div class="card">
      <h2>📋 Загальна ситуація по всіх 8 вулицях</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Вулиця</th>
              <th style="text-align: center;">Будинків</th>
              <th style="text-align: center;">Статус</th>
              <th>Деталі</th>
            </tr>
          </thead>
          <tbody>
            {''.join(street_rows)}
          </tbody>
        </table>
      </div>
    </div>

    <footer>
      Дані збираються автоматично кожні 10 хвилин з сайту ДТЕК через GitHub Actions.<br>
      <a href="active_emergencies.json">JSON аварій</a> • <a href="latest.json">Повний JSON</a> • <a href="status.json">status.json</a> • <a href="README.md">Markdown версія</a>
    </footer>
  </div>
</body>
</html>"""


def build_output(out_dir: Path, target_streets: list[str], raw_result: dict, now: dt.datetime | None = None) -> None:
    """Формує структуровані JSON файли для гілки `data`."""
    iso_utc, kyiv_str = get_kyiv_time(now)

    dtek_dir = out_dir / "dtek"
    areas_dir = dtek_dir / "areas"
    areas_dir.mkdir(parents=True, exist_ok=True)

    active_emergencies = raw_result.get("active_emergencies", [])
    streets_data = raw_result.get("streets", {})

    # 1. Повний стан (dtek/latest.json)
    full_state = {
        "metadata": {
            "source": DTEK_URL,
            "generated_utc": iso_utc,
            "generated_kyiv": kyiv_str,
            "streets_count": len(target_streets),
        },
        "system": {
            "banner": raw_result.get("banner"),
            "is_global_emergency": raw_result.get("is_global_emergency", False),
        },
        "summary": {
            "has_active_emergencies": len(active_emergencies) > 0,
            "emergencies_count": len(active_emergencies),
            "streets_with_emergencies": [
                s for s, d in streets_data.items() if d.get("has_emergency")
            ],
        },
        "active_emergencies": active_emergencies,
        "streets": streets_data,
    }
    write_json(dtek_dir / "latest.json", full_state, pretty=True)

    # 2. Масив лише активних аварій (dtek/active_emergencies.json)
    write_json(dtek_dir / "active_emergencies.json", active_emergencies, pretty=True)

    # 3. Компактний статус (dtek/status.json)
    status_summary = {
        "status": "EMERGENCY" if active_emergencies else "OK",
        "last_updated_kyiv": kyiv_str,
        "last_updated_utc": iso_utc,
        "emergencies_count": len(active_emergencies),
        "global_alert": raw_result.get("is_global_emergency", False),
        "streets": [
            {
                "street": s,
                "has_emergency": d.get("has_emergency", False),
                "emergencies_count": d.get("emergencies_count", 0),
                "houses_total": d.get("houses_total", 0),
            }
            for s, d in streets_data.items()
        ],
    }
    write_json(dtek_dir / "status.json", status_summary, pretty=True)

    # 4. Файл конкретного масиву (dtek/areas/pivnichno-brovarskyi.json)
    area_file = {
        "area": "pivnichno-brovarskyi",
        "title": "Північно-Броварський масив",
        "generated_kyiv": kyiv_str,
        "has_emergencies": len(active_emergencies) > 0,
        "emergencies": active_emergencies,
        "streets": [
            {
                "pattern": s,
                "matched_name": d.get("matched_name"),
                "has_emergency": d.get("has_emergency", False),
                "emergencies_count": d.get("emergencies_count", 0),
                "houses_total": d.get("houses_total", 0),
            }
            for s, d in streets_data.items()
        ],
    }
    write_json(areas_dir / "pivnichno-brovarskyi.json", area_file, pretty=True)

    # 5. Зручний Markdown-звіт для людей (dtek/README.md, dtek/status.md, areas/pivnichno-brovarskyi.md)
    md_content = build_markdown_dashboard(kyiv_str, streets_data, active_emergencies)
    (dtek_dir / "README.md").write_text(md_content, encoding="utf-8")
    (dtek_dir / "status.md").write_text(md_content, encoding="utf-8")
    (areas_dir / "pivnichno-brovarskyi.md").write_text(md_content, encoding="utf-8")

    # 6. Сучасна HTML-сторінка (dtek/index.html, dtek/status.html, areas/pivnichno-brovarskyi.html)
    html_content = build_html_dashboard(kyiv_str, streets_data, active_emergencies, target_streets)
    (dtek_dir / "index.html").write_text(html_content, encoding="utf-8")
    (dtek_dir / "status.html").write_text(html_content, encoding="utf-8")
    (areas_dir / "pivnichno-brovarskyi.html").write_text(html_content, encoding="utf-8")

    print(f"\n--- ПІДСУМОК ДТЕК ({kyiv_str}) ---")
    print(f"Знайдено аварій: {len(active_emergencies)}")
    for i, em in enumerate(active_emergencies, 1):
        reasons_str = "; ".join(em.get("reasons", []))
        print(f"  {i}. {em.get('matched_street')} б. {em.get('house')}: {em.get('sub_type')} ({reasons_str})")
    if not active_emergencies:
        print("  Аварійних відключень за 8 вулицями не зафіксовано.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Збір аварійних відключень ДТЕК.")
    parser.add_argument("--out", type=Path, default=Path("out"), help="Директорія виводу (data branch)")
    parser.add_argument("--areas-file", type=Path, default=Path("relay/areas.json"), help="Шлях до areas.json")
    args = parser.parse_args(argv)

    # Завантажуємо список 8 вулиць з relay/areas.json
    areas = load_areas(args.areas_file)
    pivn = areas.get("pivnichno-brovarskyi", {})
    target_streets = pivn.get("streets", [
        "вул. Малишка Андрія",
        "вул. Солов’яненка Анатолія",
        "вул. Миропільська",
        "бул. Дарницький",
        "вул. Шалетт міста",
        "вул. Юності",
        "вул. Космічна",
        "вул. Князя Романа Мстиславича",
    ])

    print(f"Моніторинг {len(target_streets)} вулиць масиву:")
    for s in target_streets:
        print(f"  - {s}")

    raw_result = scrape_dtek(target_streets)
    build_output(args.out, target_streets, raw_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
