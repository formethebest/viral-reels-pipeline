#!/usr/bin/env python3
"""viral_bank.py - банк залетевших рилсов по проекту (инструмент инстаграмщицы).

Поверх сырых данных скаута (apify_reels_scout.py --raw). Автоотбор скаута по ratio
тащит мусор (бренды по слову, рейдж-контент), поэтому отбор по теме делает человек/агент,
а скрипт даёт кандидатов, расшифровывает выбранное и копит банк.

  candidates  --raw R.json [--min-views 40000] [--top 60] [--exclude-owner a,b]
              -> таблица кандидатов по абсолютным просмотрам (owner, views, followers, ratio, date, url, подпись)
  transcribe  --raw R.json --urls URLS.txt --lang ru|en|auto --out CURATED.json
              -> расшифровка + разбор хука (Groq) для выбранных ссылок, сохранение по мере работы
  bank-add    --curated CURATED.json --bank BANK.md --market "РФ" --topic "мозг"
              -> порция СВЕРХУ в накопительный банк (ничего не удаляется), дубли по url пропускаются
"""
import argparse, json, os, re, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apify_reels_scout as s


def load_reels(raw):
    d = json.load(open(raw))
    return d["reels"] if isinstance(d, dict) else d


def cmd_candidates(a):
    excl = set(x.strip() for x in (a.exclude_owner or "").split(",") if x.strip())
    seen, rows = set(), []
    for r in sorted(load_reels(a.raw), key=lambda x: -(x.get("views") or 0)):
        if r["url"] in seen or r.get("owner") in excl or (r.get("views") or 0) < a.min_views:
            continue
        seen.add(r["url"]); rows.append(r)
        if len(rows) >= a.top:
            break
    for i, r in enumerate(rows, 1):
        cap = (r.get("caption") or "").replace("\n", " ")[:110]
        print(f'{i}\t{r.get("owner")}\t{r.get("views")}\t{r.get("followers")}\tx{r.get("ratio") or 0:.1f}\t{(r.get("ts") or "")[:10]}\t{r["url"]}\t{cap}')
    print(f"# кандидатов: {len(rows)}", file=sys.stderr)


def cmd_transcribe(a):
    s.WHISPER_LANG = a.lang
    groq = s.load_env("GROQ_API_KEY")
    if not groq:
        sys.exit("нет GROQ_API_KEY - расшифровка невозможна")
    want = [u.strip() for u in open(a.urls) if u.strip() and not u.startswith("#")]
    by_url = {r["url"]: r for r in load_reels(a.raw)}
    missing = [u for u in want if u not in by_url]
    if missing:
        print(f"⚠️ нет в сырых данных: {missing}", file=sys.stderr)
    out = []
    for i, u in enumerate([u for u in want if u in by_url], 1):
        r = dict(by_url[u])
        if not r.get("transcript"):
            r["transcript"] = s.transcribe(r, groq)
        if not r.get("analysis"):
            r["analysis"] = s.analyze_reel(r, groq)
        out.append(r)
        json.dump(out, open(a.out + ".part", "w"), ensure_ascii=False, indent=1)
        print(f"[{i}/{len(want)}] {r.get('owner')} {r.get('views')} речь={'да' if r.get('transcript') else 'нет'}", flush=True)
    os.replace(a.out + ".part", a.out)
    print(f"готово: {len(out)} в {a.out}")


def first_line(t):
    t = (t or "").strip()
    m = re.split(r"(?<=[.!?…])\s", t, maxsplit=1)
    return m[0][:220] if t else ""


def cmd_bank_add(a):
    reels = json.load(open(a.curated))
    if a.only_from:
        keep = set(re.findall(r"https://www\.instagram\.com/p/[A-Za-z0-9_-]+/", open(a.only_from).read()))
        reels = [r for r in reels if r["url"] in keep]
    old = open(a.bank).read() if os.path.exists(a.bank) else ""
    have = set(re.findall(r"https://www\.instagram\.com/p/[A-Za-z0-9_-]+/", old))
    new = [r for r in reels if r["url"] not in have]
    if not new:
        print("новых роликов нет, банк не тронут"); return
    today = datetime.date.today().isoformat()
    L = [f"## [{today}] {a.market} · {a.topic} · {len(new)} роликов\n",
         "| Ролик | Просмотры | Подписчики | Дата | Хук (первая фраза речи или подпись) |",
         "|---|---:|---:|---|---|"]
    for r in sorted(new, key=lambda x: -(x.get("views") or 0)):
        hook = first_line(r.get("transcript")) if r.get("transcript") and not s.is_hallucination(r["transcript"]) else "без речи: " + first_line(r.get("caption"))
        hook = hook.replace("|", "/")
        L.append(f'| [@{r.get("owner")}]({r["url"]}) | {r.get("views")} | {r.get("followers")} | {(r.get("ts") or "")[:10]} | {hook} |')
    L.append("")
    head = "# Банк залетевших рилсов\n\n> Накопительный: новая порция сверху, ничего не удаляется. Ведёт `viral_bank.py`.\n\n"
    body = old[len(head):] if old.startswith(head) else old
    open(a.bank, "w").write(head + "\n".join(L) + "\n" + body)
    print(f"в банк добавлено {len(new)}, пропущено дублей {len(reels) - len(new)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("candidates"); c.add_argument("--raw", required=True); c.add_argument("--min-views", type=int, default=40000)
    c.add_argument("--top", type=int, default=60); c.add_argument("--exclude-owner")
    t = sp.add_parser("transcribe"); t.add_argument("--raw", required=True); t.add_argument("--urls", required=True)
    t.add_argument("--lang", choices=["ru", "en", "auto"], default="auto"); t.add_argument("--out", required=True)
    b = sp.add_parser("bank-add"); b.add_argument("--curated", required=True); b.add_argument("--bank", required=True)
    b.add_argument("--market", required=True); b.add_argument("--topic", required=True)
    b.add_argument("--only-from", help="разбор .md: в банк только ролики, на которые он ссылается")
    a = ap.parse_args()
    {"candidates": cmd_candidates, "transcribe": cmd_transcribe, "bank-add": cmd_bank_add}[a.cmd](a)


if __name__ == "__main__":
    main()
