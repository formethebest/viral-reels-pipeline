#!/usr/bin/env python3
"""
apify_reels_scout.py — поиск ЗАЛЕТЕВШИХ Instagram-рилсов в нише + полная расшифровка.

«Залетевший» = ВЫБРОС: просмотров кратно больше, чем подписчиков у аккаунта
(reel пробил за пределы своей аудитории — алгоритм его разнёс). Крупный аккаунт
сам по себе НЕ интересен — интересен рилс-выброс, хоть из аккаунта на 1500 подписчиков.

Метрика: ratio = просмотры / подписчики. Отбираем рилсы с ratio высоким
И абсолютными просмотрами >= порога (чтобы не ловить мелочь типа 30 подписчиков → 3000 просмотров).

Пайплайн (mode=auto):
  1. user-search (apify/instagram-scraper, searchType=user) по ключевикам
     → аккаунты ниши ЛЮБОГО размера + их followersCount.
  2. reel-scraper (apify/instagram-reel-scraper) по аккаунтам → рилсы с просмотрами.
  3. ranking по ratio (выбросы), порог по абсолютным просмотрам.
  4. Groq Whisper (whisper-large-v3-turbo) по videoUrl → ПОЛНАЯ расшифровка (почти бесплатно).
  5. Markdown-отчёт.

Есть второй режим mode=youtube — те же «залетевшие» (ratio = просмотры/подписчики),
но на YouTube (Shorts + длинные) через yt-dlp БЕЗ Apify-ключа. Качаем аудио, ffmpeg
ужимает в 16кГц mp3 -> Groq Whisper. Подробности — README.md.

ИСПОЛЬЗОВАНИЕ:
  python3 apify_reels_scout.py auto \
    --keywords "фитнес дома,похудение,тренировки для женщин" \
    --niche "фитнес для женщин" \
    --search-per 18 --max-accounts 40 \
    --reels-per 15 --min-views 20000 --top 20 --transcribe 20 \
    --out out/viral-reels-YYYY-MM-DD.md \
    --raw out/viral-reels-YYYY-MM-DD.raw.json

  python3 apify_reels_scout.py youtube \
    --keywords "фитнес дома,похудение" \
    --per-keyword 20 --max-videos 50 --min-views 20000 --top 20 --transcribe 20 \
    --kind both --out out/viral-youtube-YYYY-MM-DD.md

Цены (FREE Apify): reel $0.0026, actor start $0.001, user-details ~$0.0023.
Groq whisper-large-v3-turbo ~$0.04/час → копейки.

Грабли:
  - .env с CRLF — чистим \\r.
  - run-sync таймаут 300с → тяжёлый reel-скрейп через async run+poll.
  - Whisper галлюцинирует на тишине/музыке — помечаем ⚠️.
  - Instagram videoUrl живёт недолго — расшифровываем сразу.
"""
import sys, os, json, time, re, glob, shutil, socket, ipaddress, argparse, datetime, subprocess, tempfile
import threading
import urllib.request, urllib.parse, urllib.error

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
API = "https://api.apify.com/v2"

HALLUCINATIONS = [
    "субтитры сделал", "субтитры делал", "продолжение следует",
    "спасибо за просмотр", "редактор субтитров", "корректор",
    "dimatorzok", "субтитры создавал", "amara.org",
]

def _env_lines():
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, encoding="utf-8") as f:
        return f.readlines()

def load_env(key):
    """Ключ из переменной окружения, иначе из .env рядом со скриптом."""
    if os.environ.get(key):
        return os.environ[key].strip()
    if True:
        for line in _env_lines():
            line = line.strip().replace("\r", "")
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return None

UA = "Mozilla/5.0 (compatible; reels-scout/1.0)"

def pick_apify_token():
    """Из пула APIFY_API_TOKEN, _2, _3... (переменные окружения и .env) — ключ с наибольшим остатком месяца.
    FREE-ключ без остатка отвечает на запуск актора 403 «Monthly usage hard limit exceeded»
    (так уже вставал прогон, когда брался только первый ключ). Лимиты не прочитались —
    ключ в конец очереди, но не выкидываем: лучше попытка, чем «нет токена»."""
    toks = [v.strip() for k, v in sorted(os.environ.items())
            if re.fullmatch(r"APIFY_API_TOKEN(_\d+)?", k) and v.strip()]
    if True:
        for line in _env_lines():
            line = line.strip().replace("\r", "")
            if re.match(r"APIFY_API_TOKEN(_\d+)?=", line):
                v = line.split("=", 1)[1].strip()
                if v and v not in toks:
                    toks.append(v)
    best, best_left = None, None
    for t in toks:
        try:
            req = urllib.request.Request(f"{API}/users/me/limits", headers=_api_headers(t))
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())["data"]
            left = d["limits"]["maxMonthlyUsageUsd"] - d["current"]["monthlyUsageUsd"]
        except Exception:
            left = -1e9
        if best_left is None or left > best_left:
            best, best_left = t, left
    if best is not None and best_left > -1e9:
        log(f"Apify: ключей {len(toks)}, взят с остатком ${best_left:.2f}")
    return best

# Токен Apify — в ЗАГОЛОВКЕ, не в query-строке URL (в URL он течёт в логи прокси и историю).
# Проба 19.07: GET users/me с Bearer -> OK. Но боевые вызовы — платный запуск актора и поллинг
# с нетривиальным ACL, их на пробе не проверить; поэтому при ОТКАЗЕ АВТОРИЗАЦИИ откатываемся
# на старый способ со строкой в лог. Фолбэк снять после первого удачного боевого прогона.
# ВАЖНО: триггер — только 401. 403 у Apify — это ШТАТНАЯ задержка прав на датасет после
# SUCCEEDED (см. комментарий в run_async), а не отказ авторизации: если вести 403 на фолбэк,
# токен уедет в URL почти каждый прогон (до 16 раз) и R4 закроется только на бумаге.
_BEARER_REJECTED = False   # запомнили один раз на процесс, чтобы не пробовать Bearer впустую

def _api_headers(token, extra=None):
    h = {"Authorization": f"Bearer {token}", "User-Agent": UA, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h

def _legacy_url(path, token):
    sep = "&" if "?" in path else "?"
    return f"{API}/{path}{sep}token={token}"

def _mark_bearer_rejected(path, code):
    global _BEARER_REJECTED
    if not _BEARER_REJECTED:
        log(f"Bearer-авторизация не принята на {path} (HTTP {code}) — откат на token= в URL")
    _BEARER_REJECTED = True

def api_post(path, payload, token, timeout=300):
    data = json.dumps(payload).encode()
    hdr = {"Content-Type": "application/json", "User-Agent": UA, "Accept": "application/json"}
    if not _BEARER_REJECTED:
        req = urllib.request.Request(f"{API}/{path}", data=data,
            headers=_api_headers(token, {"Content-Type": "application/json"}), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 401:      # 403 у Apify — задержка ACL, не отказ авторизации
                raise
            _mark_bearer_rejected(path, e.code)
    req = urllib.request.Request(_legacy_url(path, token), data=data, headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def api_get(path, token, timeout=60, retries=5):
    last = None
    for attempt in range(retries):
        try:
            if _BEARER_REJECTED:
                req = urllib.request.Request(_legacy_url(path, token),
                    headers={"User-Agent": UA, "Accept": "application/json"})
            else:
                req = urllib.request.Request(f"{API}/{path}", headers=_api_headers(token))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 401 and not _BEARER_REJECTED:
                # Повторяем ТУТ ЖЕ, а не через continue: run_async зовёт с retries=1, и
                # итерация цикла была бы последней — 401 улетел бы наружу вместо отката.
                _mark_bearer_rejected(path, e.code)
                req = urllib.request.Request(_legacy_url(path, token),
                    headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode())
            if e.code in (403, 429, 500, 502, 503):  # transient после завершения рана
                time.sleep(3 * (attempt + 1)); continue
            raise
    raise last

def run_async(actor, payload, token, poll=5, max_wait=900):
    run = api_post(f"acts/{actor}/runs", payload, token)["data"]
    rid = run["id"]
    waited = 0
    while waited < max_wait:
        st = api_get(f"actor-runs/{rid}", token)["data"]
        if st["status"] not in ("READY", "RUNNING"):
            break
        time.sleep(poll); waited += poll
    ds = st["defaultDatasetId"]
    # Apify после SUCCEEDED отдаёт датасет с задержкой ACL: сразу читается 403
    # insufficient-permissions (окно бывает длиннее одиночного ретрая). Патиентно
    # чередуем прямой датасет и чтение через сам run (иные права), с backoff.
    last = None
    for attempt in range(8):
        for path in (f"datasets/{ds}/items?clean=true",
                     f"actor-runs/{rid}/dataset/items?clean=true"):
            try:
                return api_get(path, token, retries=1)
            except urllib.error.HTTPError as e:
                last = e
        time.sleep(3 * (attempt + 1))
    raise last

_SECRET_RE = re.compile(
    r"(Bearer\s+)\S+"                    # заголовок авторизации
    r"|\bgsk_[A-Za-z0-9_\-]{8,}"         # ключ Groq
    r"|\bapify_api_[A-Za-z0-9_\-]{8,}"   # токен Apify
    r"|\d{8,10}:AA[A-Za-z0-9_\-]{30,}"   # токен Telegram, в т.ч. в форме botID:AA… (log() общий)
    r"|(token=)[^\s&\"']+",              # токен в query-строке (и TELEGRAM_TOKEN=…)
    re.IGNORECASE)

def _redact(text):
    """Замаскировать секреты в тексте. Полезная часть сообщения остаётся читаемой."""
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "***", str(text))

def log(*a):
    # Редактируем ВНУТРИ log(), а не по местам вызова: правило, которое надо помнить
    # применять, ломается на первой новой строке лога; механизм обойти нельзя.
    # Требование boundaries.md — «никакие токены/ключи не попадают в логи».
    print("[scout]", *(_redact(x) for x in a), file=sys.stderr, flush=True)

# ---------- недоверенный вход: ссылки на видео от Apify/Instagram/YouTube ----------
_LOCALHOST_RE = re.compile(r"^(localhost|.*\.localhost)$", re.IGNORECASE)
# Диапазоны, которые ipaddress не помечает как private, но ходить туда нам незачем.
_EXTRA_BAD_NETS = [ipaddress.ip_network("100.64.0.0/10")]  # CGNAT
RESOLVE_TIMEOUT = 5          # секунд на резолв недоверенного имени
_RESOLVE_CACHE = {}          # имя -> вердикт, на прогон (хост CDN повторяется у всех рилсов)

_HOST_CHARS_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")

def _host_is_internal(host):
    """Хост указывает во внутреннюю сеть? Адрес РАЗБИРАЕТСЯ, а не сверяется со строкой:
    строковый шаблон обходится сменой нотации (127.1, 0177.0.0.1, 2130706433, 0x7f000001,
    [::ffff:127.0.0.1], одиночный 0) — все они резолвятся в loopback. Проверено вживую.

    ДВА ПЛАСТА обхода, оба закрыты (второй нашло ревью безопасности, подтвердив живым curl):
      1. нотация адреса -> разбираем через ipaddress/inet_aton, а не сравниваем строки;
      2. ТРАНСКРИПЦИЯ хоста -> curl (собран с libidn2) и getaddrinfo разворачивают
         '１２７.0.0.1' (fullwidth-цифры), '127。0。0。1' (идеографическая точка) и
         '%31%32%37.0.0.1' (percent) в 127.0.0.1, а наши парсеры видели «доменное имя»
         и пропускали. Поэтому: сперва снимаем percent-кодирование, затем отбиваем
         ЛЮБОЙ не-ASCII хост. Интернационализированные имена CDN Instagram/YouTube
         не нужны, ложных отбоев не даёт."""
    # unquote и барьер работают В ПАРЕ, по отдельности каждый неполон: unquote снимает
    # percent-запись ДО барьера, а барьер ловит то, что осталось (в т.ч. IPv6 zone-id '%25').
    # Снимать ни то, ни другое нельзя — мутационный контроль это закрепляет.
    h = urllib.parse.unquote(host).strip()
    if not _HOST_CHARS_RE.match(h):
        return True                            # не-ASCII / пробелы / управляющие — отбить
    h = h.strip("[]").rstrip(".")              # хвостовая точка FQDN: '169.254.169.254.'
    if not h:
        return True
    if _LOCALHOST_RE.match(h):
        return True
    ip = None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:  # inet_aton понимает короткие/восьмеричные/десятичные/hex записи IPv4
            ip = ipaddress.ip_address(socket.inet_aton(h))
        except (OSError, ValueError):
            return _resolves_internal(h)   # доменное имя — смотрим, КУДА оно резолвится
    return _ip_is_internal(ip)

def _ip_is_internal(ip):
    """Адрес ведёт во внутреннюю сеть? is_loopback / is_link_local / is_unspecified для
    наших диапазонов уже покрыты is_private — это ОСОЗНАННАЯ избыточность (defense-in-depth):
    списки ipaddress между версиями Python меняются, терять loopback из-за этого не хочется.
    Мутационный контроль метит их «эквивалентными» — так и задумано, это не мёртвый код.
    А вот is_multicast и is_reserved НЕ избыточны: 224.0.0.1 и 192.0.0.1 не приватные."""
    ip = getattr(ip, "ipv4_mapped", None) or ip   # ::ffff:127.0.0.1 -> 127.0.0.1
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
            or any(ip in net for net in _EXTRA_BAD_NETS))

def _resolves_internal(host):
    """ЧЕТВЁРТЫЙ пласт обхода: имя-АЛИАС loopback. Контроль localhost-имён был неполон —
    'localhost.localdomain' прописан в /etc/hosts этой машины (проверено), под шаблон не
    подходил, IP-литералом не является => пропускался. Точечная заплатка оставила бы
    '127.0.0.1.nip.io' и прочие wildcard-резолверы, поэтому закрываем КЛАСС: резолвим имя
    и прогоняем КАЖДЫЙ полученный адрес через ту же проверку. Не резолвится — не пускаем.
    Остаточная граница после этого — только DNS-rebinding/TOCTOU (адрес подменили между
    проверкой и запросом); её честно фиксируем в спеке, закрыть можно лишь pin-ом соединения."""
    if host in _RESOLVE_CACHE:      # один CDN-хост повторяется на всех рилсах пачки
        return _RESOLVE_CACHE[host]
    verdict = _resolve_verdict(host)
    _RESOLVE_CACHE[host] = verdict
    return verdict

def _resolve_verdict(host):
    # У getaddrinfo НЕТ таймаута, и socket.setdefaulttimeout на него не действует. Хост —
    # недоверенный вход: имя с заблэкхоленным NS повесило бы резолвер на десятки секунд
    # НА КАЖДОМ рилсе (пачка из 50 -> часы вместо 5-8 минут). Поэтому — DAEMON-поток с
    # join(timeout); таймаут = отказ резолва = небезопасно (fail-closed).
    # Именно daemon и «сырой» поток: ThreadPoolExecutor в `with` на выходе ЖДЁТ зависший
    # вызов (shutdown(wait=True)), а его потоки джойнятся при выходе интерпретатора —
    # то есть таймаут бы не работал. Поймано собственным тестом (прогон встал на 30с).
    box = {}

    def _resolve():
        try:
            box["infos"] = socket.getaddrinfo(host, None)
        except BaseException as e:
            box["err"] = e

    th = threading.Thread(target=_resolve, daemon=True)
    th.start()
    th.join(RESOLVE_TIMEOUT)
    if th.is_alive() or "err" in box:
        return True
    infos = box.get("infos")
    if not infos:
        return True
    for info in infos:
        try:
            # sockaddr у AF_INET6 — 4 элемента, а адрес link-local приходит с зоной
            # ('fe80::1%eth0'), на которой ip_address бросает ValueError -> отбиваем.
            if _ip_is_internal(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            return True
    return False

def _reject_reason(url):
    """Почему ссылку отбили. Сбой резолвера НЕ должен рапортоваться как «атака»: иначе
    падение DNS на сервере даст 50 строк про «враждебные ссылки» на нормальном CDN,
    и разбираться будут с несуществующей атакой вместо настоящей причины."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if host and _RESOLVE_CACHE.get(host) is True:
        try:
            socket.inet_aton(host)
        except OSError:
            return "имя не резолвится или ведёт во внутреннюю сеть (DNS?)"
    return "небезопасная ссылка на видео"

def _safe_media_url(url):
    """Пропустить ссылку на видео к загрузчику? videoUrl/url приходят от Apify/Instagram/
    YouTube — это НЕДОВЕРЕННЫЙ вход. Отбиваем: не-http(s) схемы (file:// читал бы локальный
    файл и слал его в Groq), пустой хост, credentials в URL, литеральные внутренние адреса
    в ЛЮБОЙ нотации, строку с ведущим '-' (curl разобрал бы её как опцию) и строку, которая
    не совпадает со своей обрезанной версией (иначе проверяем одно, а в curl уходит другое).
    ГРАНИЦА (актуальна): литеральный хост разбирается, ДОМЕННОЕ ИМЯ РЕЗОЛВИТСЯ и каждый
    полученный адрес проверяется тем же предикатом. Остаток — (1) DNS-rebinding/TOCTOU:
    имя даёт публичный адрес на проверке и внутренний на самом запросе; (2) зазор
    нормализации: валидируем хост после unquote/strip/rstrip, а загрузчику отдаём исходную
    строку. Оба закрываются только пересборкой URL + pin-ом соединения на проверенный IP."""
    if not url or not isinstance(url, str):
        return False
    if url != url.strip() or url.startswith("-"):
        return False           # сверяем с ИСХОДНЫМ: валидировать одно, а слать другое — нельзя
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if p.scheme.lower() not in ("http", "https"):
        return False
    if "@" in p.netloc:        # user:pass@host — увести запрос на чужой хост/сбить парсер
        return False
    try:
        host = p.hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    return not _host_is_internal(host)

# Набор флагов curl для скачивания недоверенной ссылки. -sL следует редиректам, поэтому
# --proto (начальная схема) без --proto-redir не защищает: валидный https редиректит на file://.
CURL_SAFE = ["--proto", "=http,https", "--proto-redir", "=http,https", "--max-redirs", "5"]

def _curl_auth_cfg(key):
    """Заголовок авторизации для curl ЧЕРЕЗ КОНФИГ НА STDIN (-K -), а не через argv:
    argv виден любому пользователю машины в ps все 5-8 минут прогона."""
    return f'header = "Authorization: Bearer {key}"\n'

# ---------- step 1: accounts (любого размера) ----------
def find_accounts(keywords, per, token):
    accts = {}
    for kw in keywords:
        try:
            items = api_post("acts/apify~instagram-scraper/run-sync-get-dataset-items",
                {"search": kw, "searchType": "user", "searchLimit": per,
                 "resultsType": "details", "resultsLimit": 1}, token)
        except Exception as e:
            log("user-search fail", kw, e); continue
        for it in items:
            u = it.get("username")
            if not u: continue
            a = accts.setdefault(u, {"username": u, "followers": it.get("followersCount") or 0,
                                     "name": it.get("fullName") or "", "hits": 0})
            a["hits"] += 1
            a["followers"] = max(a["followers"], it.get("followersCount") or 0)
        log(f"keyword '{kw}': +{len(items)} (уник всего {len(accts)})")
    return list(accts.values())

# ---------- step 2: reels ----------
def scrape_reels(accounts, per, token):
    fmap = {a["username"]: a["followers"] for a in accounts}
    payload = {"username": [a["username"] for a in accounts],
               "resultsLimit": per, "skipPinnedPosts": False}
    # run-sync (как в шаге 1: актор отдаёт items прямо, без гонки прав чтения датасета)
    # — надёжный основной путь. async-фолбэк для тяжёлых скрейпов, упирающихся в 300с.
    try:
        items = api_post("acts/apify~instagram-reel-scraper/run-sync-get-dataset-items",
                         payload, token, timeout=290)
    except Exception as e:
        log("run-sync reel-scraper не прошёл, async-фолбэк:", type(e).__name__)
        items = run_async("apify~instagram-reel-scraper", payload, token)
    out, seen = [], set()
    for it in items:
        if it.get("id") in seen: continue
        seen.add(it.get("id"))
        owner = it.get("ownerUsername") or "?"
        views = it.get("videoPlayCount") or it.get("videoViewCount") or 0
        foll = fmap.get(owner, 0) or 0
        out.append({
            "views": views, "followers": foll,
            "ratio": round(views / foll, 1) if foll else 0,
            "likes": it.get("likesCount") or 0,
            "comments": it.get("commentsCount") or 0,
            "owner": owner, "url": it.get("url") or "",
            "ts": (it.get("timestamp") or "")[:10],
            "duration": it.get("videoDuration"),
            "caption": (it.get("caption") or "").strip(),
            "videoUrl": it.get("videoUrl") or "",
        })
    return out

# ---------- step 3: transcripts (Groq) ----------
# Временные файлы — СВОЙ каталог на каждый прогон (tempfile), не жёсткий /tmp/_scout_*.
# Грабли 19.07: жёсткий путь + прогон от root оставил файл за root; следующий запуск от другого пользователя
# упёрся в sticky-бит /tmp, os.remove падал, ошибка глоталась (except OSError: pass), curl
# отдавал 23 -> «провал скачивания» -> None -> в отчёте «тихо/err», будто рилс немой.
# Отсюда два правила ниже: (1) путь уникален, чужого владельца быть не может;
# (2) любая ошибка ФС ГРОМКАЯ — иначе поломка снова притворится тишиной в рилсе.
def _rm(path):
    """Удалить временный файл. Нет файла — штатно и молча. Не смогли удалить — громко."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        log(f"ОШИБКА: не могу удалить временный файл {path}: {e} "
            f"(чужой владелец / sticky-бит каталога?)")
        return False

def _cleanup(d):
    """Убрать временный каталог. Не вышло — строка в лог, но прогон пачки из 20 рилсов
    НЕ рвём: каталог свой, максимум останется мусор.
    Почему не ignore_cleanup_errors у TemporaryDirectory: на Python 3.10.12 (рабочий сервер)
    он не гасит PermissionError при неудалимом каталоге — исключение улетает наружу."""
    try:
        shutil.rmtree(d)
    except OSError as e:
        log(f"ОШИБКА: не могу убрать временный каталог {d}: {e} (останется мусор)")

def _scratch(prefix):
    """Свой временный каталог на прогон (права 0700, уникальное имя).
    Нет прав на запись / нет места — громкая ошибка и None, а НЕ тихий провал.
    Уборку делает вызывающий через _cleanup() в finally."""
    try:
        return tempfile.mkdtemp(prefix=prefix)
    except OSError as e:
        log(f"ОШИБКА: не могу создать временный каталог (prefix={prefix}) в "
            f"{tempfile.gettempdir()}: {e}. Расшифровка невозможна — проверь права и место на диске.")
        return None

WHISPER_LANG = "ru"  # --lang: ru / en / auto (auto = Whisper сам определяет язык)

def _groq_whisper(path, groq_key):
    """Файл (mp4/m4a/webm) -> текст через Groq Whisper. Groq сам достаёт аудио."""
    lang_args = [] if WHISPER_LANG == "auto" else ["-F", f"language={WHISPER_LANG}"]
    r = subprocess.run(["curl", "-s", "-K", "-",
        "https://api.groq.com/openai/v1/audio/transcriptions",
        "-F", f"file=@{path}", "-F", "model=whisper-large-v3-turbo",
        *lang_args, "-F", "response_format=json"],
        input=_curl_auth_cfg(groq_key), capture_output=True, text=True, timeout=180)
    # Последний немой путь до «тихо/err»: аудио скачали и извлекли, а Groq текст не отдал.
    # Ловим ОБА вида отказа, иначе поломка снова притворится немым рилсом (грабли 19.07):
    #   - ответ не парсится (пусто / HTML от прокси) -> исключение;
    #   - ответ ВАЛИДНЫЙ JSON с {"error": ...} (протух ключ, rate limit, кончилась квота,
    #     файл больше лимита) -> раньше .get("text","") молча отдавал "" и рилс выглядел немым.
    try:
        data = json.loads(r.stdout)
        text = (data.get("text") or "").strip()
    except Exception as e:
        log(f"ОШИБКА: Groq Whisper не отдал текст для {os.path.basename(path)} "
            f"(curl rc={r.returncode}, {type(e).__name__}): {r.stdout[:200] or r.stderr[:200]}")
        return None
    if text:
        return text
    err = data.get("error")
    detail = (err.get("message") if isinstance(err, dict) else err) or r.stdout[:200] or "пустой ответ"
    log(f"ОШИБКА: Groq Whisper вернул ответ без текста для {os.path.basename(path)} "
        f"(curl rc={r.returncode}): {detail}")
    return None

def _extract_audio(src, mp3):
    """Видео -> маленький mp3 через ffmpeg (Groq Whisper надёжнее на аудио; целый mp4
    часто отклоняется/перебор 25МБ). """
    if not _rm(mp3):
        return False
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame",
                    "-q:a", "4", mp3], capture_output=True, timeout=120)
    if os.path.exists(mp3) and os.path.getsize(mp3) > 0:
        return True
    log(f"ОШИБКА: ffmpeg не отдал аудио из {src} — расшифровка этого рилса пропущена")
    return False

def transcribe(reel, groq_key, dl_tries=3):
    """Рилс IG -> текст. CDN-видео (curl) -> ВЫТАСКИВАЕМ АУДИО (ffmpeg) -> Whisper.
    Метод старой автоматизации: слать в Whisper аудио-mp3, не целый mp4. yt-dlp для IG
    с сервера заблокирован — фолбэк не держим (только зря висит), полагаемся на CDN."""
    if not reel.get("videoUrl"):
        return None
    if not _safe_media_url(reel["videoUrl"]):
        log(f"ОШИБКА: {_reject_reason(reel['videoUrl'])} у @{reel.get('owner', '?')}, "
            f"рилс пропущен: {reel['videoUrl'][:120]!r}")
        return None
    d = _scratch("scout_reel_")
    if d is None:
        return None
    try:
        mp4 = os.path.join(d, "reel.mp4")
        mp3 = os.path.join(d, "reel.mp3")
        for attempt in range(dl_tries):
            if not _rm(mp4):
                return None
            dl = subprocess.run(["curl", "-sL", *CURL_SAFE, "-o", mp4,
                "-H", "User-Agent: Mozilla/5.0", "--", reel["videoUrl"]], timeout=120)
            size = os.path.getsize(mp4) if os.path.exists(mp4) else 0
            if dl.returncode == 0 and size >= 1000:
                if _extract_audio(mp4, mp3):
                    return _groq_whisper(mp3, groq_key)
                return None
            log(f"  ОШИБКА: скачивание не удалось (попытка {attempt+1}/{dl_tries}, "
                f"curl rc={dl.returncode}, файл {size} байт) @{reel.get('owner', '?')}")
            time.sleep(2)
    finally:
        _cleanup(d)  # уборка и на исключении, и на любом return выше
    return None

def first_line(caption, n=140):
    if not caption: return "(без подписи)"
    line = re.sub(r"\s+", " ", caption.splitlines()[0])
    return (line[:n] + "…") if len(line) > n else line

def is_hallucination(txt):
    if not txt or len(txt) < 15: return True
    low = txt.lower()
    # Галлюцинация на тишине/музыке = КОРОТКИЙ вывод, целиком из служебной фразы.
    # Длинная речь, где «спасибо за просмотр» лишь в аутро, — это речь, не флагуем
    # (иначе любое длинное видео с прощанием в конце ошибочно попадёт в «без речи»).
    if len(txt) < 220:
        return any(h in low for h in HALLUCINATIONS)
    return False

def kfmt(n):
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else (f"{n/1000:.0f}k" if n >= 1000 else str(n))

# ---------- step 3.5: разбор хука (Groq chat) ----------
NICHE = ""  # --niche: тема/ниша, под которую разбираем хуки

ANALYZE_PROMPT = """Ты аналитик вирального контента для ниши: {niche}.
Рилс набрал {views} просмотров (×{ratio} к числу подписчиков — это «залёт»).

Подпись: {caption}

Транскрипт речи:
{transcript}

Дай короткий разбор на русском, РОВНО 4 строки, каждая 1 предложение, без воды:
Хук (оценка X/10): что цепляет в первые 3 секунды.
Приём: какой приём удержания/вовлечения использован.
Идея: главная мысль или оффер.
Забрать себе: 1 конкретная рекомендация, как это применить в рилсе в нише «{niche}»."""

def _groq_chat(prompt, groq_key, model="llama-3.3-70b-versatile"):
    payload = json.dumps({"model": model, "temperature": 0.4, "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]})
    r = subprocess.run(["curl", "-s", "-K", "-",
        "https://api.groq.com/openai/v1/chat/completions",
        "-H", "Content-Type: application/json", "-d", payload],
        input=_curl_auth_cfg(groq_key), capture_output=True, text=True, timeout=90)
    try:
        return json.loads(r.stdout)["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

def analyze_reel(reel, groq_key):
    """Разбор хука по транскрипту (оценка + приём + идея + рекомендация). Нет речи -> подсказка."""
    tr = reel.get("transcript")
    if not tr or is_hallucination(tr):
        return "(нет распознанной речи — оценивай по подписи и визуалу)"
    prompt = ANALYZE_PROMPT.format(niche=NICHE or "своя ниша автора",
        views=kfmt(reel.get("views") or 0), ratio=reel.get("ratio") or 0,
        caption=(reel.get("caption") or "—")[:300], transcript=tr[:3000])
    return _groq_chat(prompt, groq_key) or "(разбор не удался)"

# ---------- report ----------
def build_report(kws, accounts, reels, top, today, min_views):
    L = []
    L.append(f"# Залетевшие рилсы{(' — ' + NICHE) if NICHE else ''} — Apify + Groq ({today})")
    L.append("")
    L.append(f"Ключевики: {', '.join(kws)}")
    L.append(f"Аккаунтов ниши: {len(accounts)} · рилсов собрано: {len(reels)} · "
             f"порог просмотров: {kfmt(min_views)} · в отчёте топ-{len(top)} по «залёту» (просмотры/подписчики)")
    L.append("")
    L.append("«Залёт» = во сколько раз просмотры превысили число подписчиков аккаунта. ×10 и выше — рилс пробил аудиторию.")
    L.append("")
    L.append("## Сводка")
    L.append("")
    L.append("| # | 🚀 залёт | 👁 просмотры | подписч. | ❤️ | 💬 | сек | аккаунт | хук | ссылка |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(top, 1):
        L.append(f"| {i} | ×{r['ratio']:g} | {kfmt(r['views'])} | {kfmt(r['followers'])} | "
                 f"{kfmt(r['likes'])} | {kfmt(r['comments'])} | {r.get('duration') or '?'} | "
                 f"@{r['owner']} | {first_line(r['caption']).replace('|','/')} | {r['url']} |")
    L.append("")
    L.append("## Полные расшифровки")
    L.append("")
    for i, r in enumerate(top, 1):
        L.append(f"### {i}. @{r['owner']} — {kfmt(r['views'])} просмотров (×{r['ratio']:g} к {kfmt(r['followers'])} подписчиков)")
        L.append(f"- Ссылка: {r['url']} · {r['ts']} · {r.get('duration') or '?'}с · "
                 f"❤️{kfmt(r['likes'])} 💬{kfmt(r['comments'])}")
        L.append(f"- **Подпись:** {(r['caption'] or '(без подписи)')[:600]}")
        tr = r.get("transcript")
        if tr is None:
            L.append("- **Расшифровка:** _(не получена)_")
        elif is_hallucination(tr):
            L.append(f"- **Расшифровка:** ⚠️ без речи (музыка/текст на экране): \"{tr[:120]}\"")
        else:
            L.append(f"- **Расшифровка:**\n\n> {tr}")
        if r.get("analysis"):
            L.append(f"- **Разбор хука:**\n\n> {r['analysis']}")
        L.append("")
    return "\n".join(L)

# ==================== YOUTUBE (yt-dlp, без API-ключа) ====================
# Тот же принцип «залёта», что и для рилсов: ratio = просмотры / подписчики канала.
# YouTube идёт МИМО Apify — yt-dlp ищет, отдаёт статистику и качает аудио (как в pain_miner.py).

def yt_search(keywords, per):
    """flat-поиск: быстро собираем кандидатов (id, просмотры, длительность, канал)."""
    cands = {}
    for kw in keywords:
        try:
            r = subprocess.run(["yt-dlp", "-J", "--flat-playlist", "--no-warnings",
                "--ignore-errors", f"ytsearch{per}:{kw}"],
                capture_output=True, text=True, timeout=240)
            data = json.loads(r.stdout)
        except Exception as e:
            log("yt-search fail", kw, e); continue
        n = 0
        for e in data.get("entries") or []:
            vid = e.get("id")
            if not vid or len(vid) != 11 or vid in cands: continue
            cands[vid] = {
                "video_id": vid,
                "title": e.get("title") or "",
                "owner": e.get("channel") or e.get("uploader") or "",
                "views_flat": e.get("view_count") or 0,
                "duration": e.get("duration") or 0,
            }
            n += 1
        log(f"keyword '{kw}': +{n} (уник всего {len(cands)})")
    return list(cands.values())

def yt_enrich(cand, tries=2):
    """full -J одного видео: точные просмотры + подписчики канала + лайки/комменты/дата."""
    url = f"https://www.youtube.com/watch?v={cand['video_id']}"
    for _ in range(tries):
        try:
            r = subprocess.run(["yt-dlp", "-J", "--skip-download", "--no-warnings",
                "--no-playlist", url], capture_output=True, text=True, timeout=120)
            d = json.loads(r.stdout)
        except Exception:
            time.sleep(2); continue
        views = d.get("view_count") or cand.get("views_flat") or 0
        subs = d.get("channel_follower_count") or 0
        dur = d.get("duration") or cand.get("duration") or 0
        up = d.get("upload_date") or ""
        ts = f"{up[:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else ""
        return {
            "views": views, "followers": subs,
            "ratio": round(views / subs, 1) if subs else 0,
            "likes": d.get("like_count") or 0,
            "comments": d.get("comment_count") or 0,
            "owner": d.get("channel") or d.get("uploader") or cand.get("owner") or "?",
            "url": d.get("webpage_url") or url,
            "ts": ts, "duration": dur,
            "kind": "Shorts" if dur and dur <= 180 else "видео",  # эвристика по длительности
            "title": d.get("title") or cand.get("title") or "",
            "caption": (d.get("description") or "").strip(),
            "video_id": cand["video_id"],
        }
    return None

GROQ_MAX = 24 * 1024 * 1024  # лимит загрузки Groq free ~25МБ, берём с запасом

def transcribe_yt(video, groq_key, dl_tries=2):
    """yt-dlp качает аудио, ffmpeg ужимает в 16кГц моно mp3 48k. Длинное видео (31 мин)
    в bestaudio = 32МБ (Groq режет на 25), после сжатия ~10МБ. Whisper по 16кГц речь читает
    без потери качества. Если даже сжатое > лимита (часовые) — честно пропускаем."""
    # У yt-dlp НЕТ флага --proto (проверено на машине), поэтому здесь _safe_media_url —
    # ЕДИНСТВЕННЫЙ контроль схемы, а не дублирующий слой. Снимать нельзя.
    if not _safe_media_url(video.get("url") or ""):
        log(f"ОШИБКА: небезопасная ссылка на видео, пропуск: {str(video.get('url'))[:120]!r}")
        return None
    d = _scratch("scout_yt_")
    if d is None:
        return None
    try:
        out = os.path.join(d, "audio.mp3")
        tmpl = os.path.join(d, "audio.%(ext)s")
        for attempt in range(dl_tries):
            for p in glob.glob(os.path.join(d, "audio.*")):
                if not _rm(p):
                    return None
            r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--no-playlist",
                "-f", "wa/worstaudio/ba/bestaudio/18/worst",
                "-x", "--audio-format", "mp3",
                "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1 -b:a 48k",
                "-o", tmpl, "--", video["url"]],
                capture_output=True, text=True, timeout=300)
            size = os.path.getsize(out) if os.path.exists(out) else 0
            if r.returncode == 0 and size >= 1000:
                if size > GROQ_MAX:
                    log(f"  аудио {size//1024//1024}МБ > лимита Groq — пропуск расшифровки")
                    return None
                return _groq_whisper(out, groq_key)
            log(f"  ОШИБКА: yt-dlp не отдал аудио (попытка {attempt+1}/{dl_tries}, "
                f"rc={r.returncode}, файл {size} байт) {video.get('url', '?')}")
            time.sleep(2)
    finally:
        _cleanup(d)  # уборка и на исключении, и на любом return выше
    return None

def build_report_yt(kws, vids, top, today, min_views):
    L = []
    L.append(f"# Залетевшие YouTube-видео — yt-dlp + Groq ({today})")
    L.append("")
    L.append(f"Ключевики: {', '.join(kws)}")
    L.append(f"Видео со статистикой: {len(vids)} · порог просмотров: {kfmt(min_views)} · "
             f"в отчёте топ-{len(top)} по «залёту» (просмотры/подписчики)")
    L.append("")
    L.append("«Залёт» = во сколько раз просмотры превысили число подписчиков канала. ×10 и выше — видео пробило аудиторию. Тип Shorts/видео — по длительности (≤3 мин = Shorts).")
    L.append("")
    L.append("## Сводка")
    L.append("")
    L.append("| # | 🚀 залёт | 👁 просмотры | подписч. | ❤️ | 💬 | сек | тип | канал | заголовок | ссылка |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, v in enumerate(top, 1):
        L.append(f"| {i} | ×{v['ratio']:g} | {kfmt(v['views'])} | {kfmt(v['followers'])} | "
                 f"{kfmt(v['likes'])} | {kfmt(v['comments'])} | {v.get('duration') or '?'} | "
                 f"{v['kind']} | {v['owner'].replace('|','/')} | "
                 f"{first_line(v['title']).replace('|','/')} | {v['url']} |")
    L.append("")
    L.append("## Полные расшифровки")
    L.append("")
    for i, v in enumerate(top, 1):
        L.append(f"### {i}. {v['owner']} — {kfmt(v['views'])} просмотров (×{v['ratio']:g} к {kfmt(v['followers'])} подписчиков) · {v['kind']}")
        L.append(f"- **{v['title']}**")
        L.append(f"- Ссылка: {v['url']} · {v['ts']} · {v.get('duration') or '?'}с · "
                 f"❤️{kfmt(v['likes'])} 💬{kfmt(v['comments'])}")
        if v.get("caption"):
            L.append(f"- **Описание:** {v['caption'][:600]}")
        tr = v.get("transcript")
        if tr is None:
            L.append("- **Расшифровка:** _(не получена)_")
        elif is_hallucination(tr):
            L.append(f"- **Расшифровка:** ⚠️ без речи (музыка/текст на экране): \"{tr[:120]}\"")
        else:
            L.append(f"- **Расшифровка:**\n\n> {tr}")
        if v.get("analysis"):
            L.append(f"- **Разбор хука:**\n\n> {v['analysis']}")
        L.append("")
    return "\n".join(L)

def run_youtube(a, groq):
    kws = [k.strip() for k in a.keywords.split(",") if k.strip()]
    today = datetime.date.today().isoformat()

    log("STEP 1: поиск видео на YouTube (yt-dlp, без ключа)…")
    cands = yt_search(kws, a.per_keyword)
    if not cands: sys.exit("видео не найдены")
    # приоритет по «грубым» просмотрам из flat-поиска, режем хвост перед дорогим обогащением
    cands.sort(key=lambda x: x.get("views_flat") or 0, reverse=True)
    cands = cands[:a.max_videos]
    log(f"кандидатов в работу: {len(cands)}")

    log(f"STEP 2: сбор статистики (просмотры + подписчики) по {len(cands)} видео…")
    vids = []
    for i, c in enumerate(cands):
        v = yt_enrich(c)
        if v: vids.append(v)
        if (i + 1) % 10 == 0: log(f"  обогащено {i+1}/{len(cands)}")
    log(f"со статистикой: {len(vids)}")
    if a.kind == "shorts": vids = [v for v in vids if v["kind"] == "Shorts"]
    elif a.kind == "long": vids = [v for v in vids if v["kind"] == "видео"]

    # выбросы: просмотры >= порога И ratio >= порога; порог авто-снижается (как в рилсах)
    cutoff = ((datetime.date.today() - datetime.timedelta(days=a.max_age_days)).isoformat()
              if a.max_age_days else "")
    def passes_yt(v, fl):
        return (v["views"] >= fl and v["ratio"] >= a.min_ratio
                and (v.get("followers") or 0) >= a.min_followers
                and (not cutoff or (v.get("ts") or "") >= cutoff))
    floor = a.min_views
    pool = [v for v in vids if passes_yt(v, floor)]
    while len(pool) < a.top and floor > 2000:
        floor = int(floor / 2)
        pool = [v for v in vids if passes_yt(v, floor)]
        log(f"мало выбросов, снижаю порог просмотров -> {kfmt(floor)} (найдено {len(pool)})")
    if a.min_followers:
        log(f"фильтр: только каналы от {kfmt(a.min_followers)} подписчиков")
    if cutoff:
        log(f"фильтр: только видео с {cutoff} (не старше {a.max_age_days} дней)")
    pool.sort(key=lambda x: x["ratio"], reverse=True)
    top = pool[:a.top]
    if top:
        log(f"топ-залёт: ×{top[0]['ratio']:g} {top[0]['owner']} ({kfmt(top[0]['views'])} просм)")

    log(f"STEP 3: расшифровка топ-{min(a.transcribe, len(top))} через Groq…")
    for i, v in enumerate(top[:a.transcribe]):
        v["transcript"] = transcribe_yt(v, groq) if groq else None
        mark = "ok" if v.get("transcript") and not is_hallucination(v["transcript"]) else "тихо/err"
        log(f"  [{i+1}] {v['owner']} ×{v['ratio']:g} {kfmt(v['views'])} -> {mark}")
        if a.analyze and groq:
            v["analysis"] = analyze_reel(v, groq)

    if a.raw:
        json.dump({"videos": sorted(vids, key=lambda x: x['ratio'], reverse=True)},
                  open(a.raw, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    report = build_report_yt(kws, vids, top, today, floor)
    open(a.out, "w", encoding="utf-8").write(report + "\n")
    log(f"ГОТОВО -> {a.out}")
    print(report[:1800])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["auto", "youtube"])
    ap.add_argument("--keywords", required=True)
    ap.add_argument("--search-per", type=int, default=18)
    ap.add_argument("--max-accounts", type=int, default=40)
    ap.add_argument("--reels-per", type=int, default=15)
    ap.add_argument("--min-views", type=int, default=20000, help="порог абсолютных просмотров")
    ap.add_argument("--min-ratio", type=float, default=3.0, help="минимальный коэф. залёта")
    ap.add_argument("--min-followers", type=int, default=0, help="минимум подписчиков аккаунта (режет микро с раздутым ratio)")
    ap.add_argument("--max-age-days", type=int, default=0, help="только рилсы не старше N дней (0 = без фильтра по дате)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--transcribe", type=int, default=20)
    ap.add_argument("--lang", choices=["ru", "en", "auto"], default="ru", help="язык расшифровки Whisper (auto = определить сам)")
    ap.add_argument("--analyze", action="store_true", help="разбор хука по транскрипту через Groq (оценка/приём/идея/рекомендация)")
    ap.add_argument("--niche", default="", help="ниша/тема для разбора хуков и заголовка отчёта, напр. \"фитнес для женщин\"")
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw", default=None)
    # --- только для mode=youtube ---
    ap.add_argument("--per-keyword", type=int, default=20, help="[youtube] результатов поиска на ключевик")
    ap.add_argument("--max-videos", type=int, default=50, help="[youtube] сколько видео обогащать статистикой")
    ap.add_argument("--kind", choices=["shorts", "long", "both"], default="both", help="[youtube] формат")
    a = ap.parse_args()
    global WHISPER_LANG, NICHE
    WHISPER_LANG = a.lang
    NICHE = a.niche.strip()

    groq = load_env("GROQ_API_KEY")

    if a.mode == "youtube":
        if not groq: log("⚠️ нет GROQ_API_KEY — расшифровок не будет, только метрики")
        run_youtube(a, groq)
        return

    apify = pick_apify_token()
    if not apify: sys.exit("нет APIFY_API_TOKEN - возьми ключ на apify.com (Settings -> API & Integrations) и впиши в .env")
    kws = [k.strip() for k in a.keywords.split(",") if k.strip()]
    today = datetime.date.today().isoformat()

    log("STEP 1: аккаунты ниши (любой размер)…")
    accounts = find_accounts(kws, a.search_per, apify)
    # приоритет аккаунтам, что всплыли по нескольким ключевикам, затем по охвату; режем хвост
    accounts.sort(key=lambda x: (x["hits"], x["followers"]), reverse=True)
    accounts = accounts[:a.max_accounts]
    log(f"аккаунтов в работу: {len(accounts)}")
    if not accounts: sys.exit("аккаунты не найдены")

    log("STEP 2: скрейп рилсов…")
    reels = scrape_reels(accounts, a.reels_per, apify)
    log(f"собрано рилсов: {len(reels)}")

    # выбросы: просмотры/ratio/подписчики >= порогов И (опц.) не старше N дней; сортировка по ratio
    cutoff = ((datetime.date.today() - datetime.timedelta(days=a.max_age_days)).isoformat()
              if a.max_age_days else "")
    def passes(r, fl):
        return (r["views"] >= fl and r["ratio"] >= a.min_ratio
                and (r.get("followers") or 0) >= a.min_followers
                and (not cutoff or (r.get("ts") or "") >= cutoff))
    floor = a.min_views
    pool = [r for r in reels if passes(r, floor)]
    while len(pool) < a.top and floor > 2000:
        floor = int(floor / 2)
        pool = [r for r in reels if passes(r, floor)]
        log(f"мало выбросов, снижаю порог просмотров -> {kfmt(floor)} (найдено {len(pool)})")
    if a.min_followers:
        log(f"фильтр: только аккаунты от {kfmt(a.min_followers)} подписчиков")
    if cutoff:
        log(f"фильтр: только рилсы с {cutoff} (не старше {a.max_age_days} дней)")
    pool.sort(key=lambda x: x["ratio"], reverse=True)
    top = pool[:a.top]
    if top:
        log(f"топ-залёт: ×{top[0]['ratio']:g} @{top[0]['owner']} ({kfmt(top[0]['views'])} просм)")

    log(f"STEP 3: расшифровка топ-{min(a.transcribe, len(top))} через Groq…")
    for i, r in enumerate(top[:a.transcribe]):
        r["transcript"] = transcribe(r, groq) if groq else None
        mark = "ok" if r.get("transcript") and not is_hallucination(r["transcript"]) else "тихо/err"
        log(f"  [{i+1}] @{r['owner']} ×{r['ratio']:g} {kfmt(r['views'])} -> {mark}")
        if a.analyze and groq:
            r["analysis"] = analyze_reel(r, groq)

    if a.raw:
        json.dump({"accounts": accounts, "reels": sorted(reels, key=lambda x: x['ratio'], reverse=True)},
                  open(a.raw, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    report = build_report(kws, accounts, reels, top, today, floor)
    open(a.out, "w", encoding="utf-8").write(report + "\n")
    log(f"ГОТОВО -> {a.out}")
    print(report[:1800])

if __name__ == "__main__":
    main()
