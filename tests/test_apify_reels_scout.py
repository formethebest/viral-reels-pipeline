#!/usr/bin/env python3
"""
test_apify_reels_scout.py — регрессия на «залипание временного файла» (баг 19.07).

Диагноз: transcribe() писала в жёсткий /tmp/_scout_reel.mp4. Прогон 15.07 от root оставил
файл за root; запуск от другого пользователя упёрся в sticky-бит /tmp — os.remove падал, ошибка глоталась
(except OSError: pass), curl отдавал 23, скрипт считал это провалом скачивания и возвращал
None. В отчёте печаталось «тихо/err» — выглядело как немой рилс, хотя ключи/ffmpeg/Groq живы.

Сеть здесь не используется: subprocess.run подменяется фейком, наружу не ходим.
Прогон: python3 -m unittest discover -s tests -v
"""
import glob
import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apify_reels_scout as scout  # noqa: E402


_dns_patch = None


def setUpModule():
    """Шаг step8 добавил проверку «куда резолвится имя». Этот набор про ВРЕМЕННЫЕ ФАЙЛЫ,
    поэтому даём разрешающий резолвер-заглушку — и в сеть не ходим, и фикстуры живут."""
    global _dns_patch
    _dns_patch = mock.patch.object(
        scout.socket, "getaddrinfo", lambda host, *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 0))])
    _dns_patch.start()


def tearDownModule():
    if _dns_patch is not None:
        _dns_patch.stop()


@contextmanager
def captured_stderr():
    """log() пишет в stderr — ловим, чтобы проверять громкость ошибок."""
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = old


# Баг 19.07 родился из смеси прогонов root/другой пользователь, поэтому набор обязан честно вести себя
# под обоими. root игнорирует права доступа — моделировать «нельзя записать» через chmod
# под ним бессмысленно, такие проверки пропускаем явно, а не делаем вид, что прогнали.
skip_under_root = unittest.skipIf(
    os.geteuid() == 0, "под root права игнорируются — запрет на запись не смоделировать")


@contextmanager
def unwritable_tmpdir():
    """Смоделированный недоступный для записи временный путь (ядро регрессии)."""
    holder = tempfile.mkdtemp(prefix="scout_ro_holder_")
    locked = os.path.join(holder, "locked")
    os.mkdir(locked)
    os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)  # r-x: создать внутри нельзя
    old = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = locked
    tempfile.tempdir = locked
    try:
        yield locked
    finally:
        tempfile.tempdir = None
        if old is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old
        os.chmod(locked, stat.S_IRWXU)
        os.rmdir(locked)
        os.rmdir(holder)


def fake_run_factory(seen, *, curl_rc=0, make_file=True, size=5000):
    """Подменяет subprocess.run: curl/ffmpeg/yt-dlp не запускаются, файлы создаются на месте."""
    def fake_run(argv, **kw):
        seen.append(list(argv))
        tool = os.path.basename(argv[0])
        if tool == "curl" and "-o" in argv:
            target = argv[argv.index("-o") + 1]
            if make_file:
                with open(target, "wb") as f:
                    f.write(b"\0" * size)
            return mock.Mock(returncode=curl_rc, stdout="", stderr="")
        if tool == "ffmpeg":
            target = argv[-1]
            if make_file:
                with open(target, "wb") as f:
                    f.write(b"\0" * size)
            return mock.Mock(returncode=0, stdout="", stderr="")
        if tool == "yt-dlp":
            tmpl = argv[argv.index("-o") + 1]
            if make_file:
                with open(tmpl.replace("%(ext)s", "mp3"), "wb") as f:
                    f.write(b"\0" * size)
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    return fake_run


REEL = {"videoUrl": "https://cdn.example/reel.mp4", "owner": "acct"}
VIDEO = {"url": "https://youtube.example/watch?v=x", "owner": "chan"}


class TestUniqueTempPaths(unittest.TestCase):
    """R1: уникальный временный файл на каждый прогон, никаких жёстких /tmp/_scout_*."""

    def test_transcribe_uses_unique_path_per_run(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", return_value="речь"):
            self.assertEqual(scout.transcribe(REEL, "k"), "речь")
            self.assertEqual(scout.transcribe(REEL, "k"), "речь")
        curls = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "curl"]
        self.assertEqual(len(curls), 2)
        self.assertNotEqual(curls[0], curls[1], "два прогона делят один путь — баг вернулся")
        for p in curls:
            self.assertNotIn("_scout_reel.mp4", p, "жёсткий путь /tmp/_scout_reel.mp4 остался")

    def test_transcribe_yt_uses_unique_path_per_run(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", return_value="речь"):
            self.assertEqual(scout.transcribe_yt(VIDEO, "k"), "речь")
            self.assertEqual(scout.transcribe_yt(VIDEO, "k"), "речь")
        tmpls = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "yt-dlp"]
        self.assertEqual(len(tmpls), 2)
        self.assertNotEqual(tmpls[0], tmpls[1], "два прогона делят один путь — баг вернулся")
        for p in tmpls:
            self.assertNotIn("/tmp/_scout_yt.", p, "жёсткий путь /tmp/_scout_yt.* остался")

    def test_transcribe_cleans_up_after_itself(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", return_value="речь"):
            scout.transcribe(REEL, "k")
        path = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "curl"][0]
        self.assertFalse(os.path.exists(os.path.dirname(path)), "временный каталог не убран")

    def test_transcribe_cleans_up_even_when_whisper_raises(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                scout.transcribe(REEL, "k")
        path = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "curl"][0]
        self.assertFalse(os.path.exists(os.path.dirname(path)), "мусор остался после исключения")


    def test_transcribe_yt_cleans_up_after_itself(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", return_value="речь"):
            scout.transcribe_yt(VIDEO, "k")
        tmpl = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "yt-dlp"][0]
        self.assertFalse(os.path.exists(os.path.dirname(tmpl)), "временный каталог YT не убран")

    def test_transcribe_yt_cleans_up_even_when_whisper_raises(self):
        seen = []
        with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen)), \
             mock.patch.object(scout, "_groq_whisper", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                scout.transcribe_yt(VIDEO, "k")
        tmpl = [a[a.index("-o") + 1] for a in seen if os.path.basename(a[0]) == "yt-dlp"][0]
        self.assertFalse(os.path.exists(os.path.dirname(tmpl)), "мусор YT остался после исключения")


class TestLoudFailures(unittest.TestCase):
    """R2/R4/R5: провал виден в логе, а не маскируется под немой рилс («тихо/err»)."""

    @skip_under_root
    def test_unwritable_tmpdir_logs_error_not_silent_none(self):
        with unwritable_tmpdir(), captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run", fake_run_factory([])):
                out = scout.transcribe(REEL, "k")
        self.assertIsNone(out)
        self.assertIn("ОШИБКА", err.getvalue(),
                      "недоступный для записи временный путь молчит — это и был баг 19.07")

    @skip_under_root
    def test_unwritable_tmpdir_logs_error_in_transcribe_yt(self):
        with unwritable_tmpdir(), captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run", fake_run_factory([])):
                out = scout.transcribe_yt(VIDEO, "k")
        self.assertIsNone(out)
        self.assertIn("ОШИБКА", err.getvalue())

    def test_download_failure_is_logged(self):
        seen = []
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   fake_run_factory(seen, curl_rc=23, make_file=False)), \
                 mock.patch.object(scout.time, "sleep"):
                out = scout.transcribe(REEL, "k", dl_tries=1)
        self.assertIsNone(out)
        self.assertIn("23", err.getvalue(), "returncode curl не попал в лог")

    def test_yt_download_failure_is_logged(self):
        seen = []
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   fake_run_factory(seen, make_file=False)), \
                 mock.patch.object(scout.time, "sleep"):
                out = scout.transcribe_yt(VIDEO, "k", dl_tries=1)
        self.assertIsNone(out)
        self.assertIn("ОШИБКА", err.getvalue(), "провал yt-dlp немой — маскируется под «тихо/err»")

    def test_yt_oversize_audio_is_logged_and_skipped(self):
        seen = []
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   fake_run_factory(seen, size=scout.GROQ_MAX + 1)), \
                 mock.patch.object(scout, "_groq_whisper", return_value="речь"):
                out = scout.transcribe_yt(VIDEO, "k", dl_tries=1)
        self.assertIsNone(out)
        self.assertIn("лимита Groq", err.getvalue())

    def test_groq_failure_is_logged(self):
        """Аудио есть, Groq не ответил — последний немой путь до «тихо/err»."""
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   return_value=mock.Mock(returncode=7, stdout="", stderr="boom")):
                out = scout._groq_whisper("/nonexistent/audio.mp3", "k")
        self.assertIsNone(out)
        self.assertIn("ОШИБКА", err.getvalue())

    def test_groq_error_json_is_logged_not_silent_empty(self):
        """Самый частый боевой отказ: протух ключ / rate limit — Groq отвечает ВАЛИДНЫМ JSON
        с error. Раньше .get("text","") молча отдавал "" и рилс выглядел немым."""
        body = '{"error":{"message":"Invalid API Key","type":"invalid_request_error"}}'
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   return_value=mock.Mock(returncode=0, stdout=body, stderr="")):
                out = scout._groq_whisper("/nonexistent/audio.mp3", "k")
        self.assertIsNone(out, "вернулась пустая строка вместо None — отказ Groq снова немой")
        self.assertIn("ОШИБКА", err.getvalue())
        self.assertIn("Invalid API Key", err.getvalue(), "причина отказа не доехала до лога")

    def test_download_rc_zero_but_empty_file_is_logged(self):
        """R4 требует ОБЕ половины: rc==0 И size>=1000. CDN может отдать rc=0 и 10 байт."""
        seen = []
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run",
                                   fake_run_factory(seen, curl_rc=0, size=10)), \
                 mock.patch.object(scout.time, "sleep"):
                out = scout.transcribe(REEL, "k", dl_tries=1)
        self.assertIsNone(out)
        self.assertIn("ОШИБКА", err.getvalue())
        self.assertIn("10 байт", err.getvalue())

    def test_yt_download_rc_zero_but_empty_file_is_logged(self):
        seen = []
        with captured_stderr() as err:
            with mock.patch.object(scout.subprocess, "run", fake_run_factory(seen, size=10)), \
                 mock.patch.object(scout, "_groq_whisper", return_value="речь"), \
                 mock.patch.object(scout.time, "sleep"):
                out = scout.transcribe_yt(VIDEO, "k", dl_tries=1)
        self.assertIsNone(out)
        # Ассертим ИМЕННО причину: иначе тест зеленеет через посторонний путь отказа
        # (незамоканный _groq_whisper тоже пишет «ОШИБКА») и мутант выживает.
        self.assertIn("yt-dlp не отдал аудио", err.getvalue())
        self.assertIn("10 байт", err.getvalue())

    @skip_under_root
    def test_cleanup_failure_does_not_kill_the_run(self):
        """R7: неудача уборки — это мусор в своём каталоге, а не исключение посреди пачки
        из 20 рилсов. Сбой уборки настоящий: каталог с файлом делаем неочищаемым (r-x)."""
        holder = tempfile.mkdtemp(prefix="scout_holder_")
        old_tmp = tempfile.tempdir
        tempfile.tempdir = holder
        try:
            d = scout._scratch("scout_cleanup_")
            self.assertIsNotNone(d)
            open(os.path.join(d, "stuck.bin"), "w").close()
            # Запрещаем удаление в РОДИТЕЛЕ: сбой уборки настоящий, а не смоделированный
            # моком того самого механизма, который проверяем.
            os.chmod(holder, stat.S_IRUSR | stat.S_IXUSR)
            with captured_stderr() as err:
                try:
                    scout._cleanup(d)
                except OSError as e:
                    self.fail(f"сбой уборки вылетел наружу и оборвал бы прогон пачки: {e}")
            os.chmod(holder, stat.S_IRWXU)
            self.assertTrue(os.path.isdir(d),
                            "каталог всё же убрался — сбой уборки не смоделирован, тест холостой")
            self.assertIn("ОШИБКА", err.getvalue(), "сбой уборки прошёл молча")
        finally:
            tempfile.tempdir = old_tmp
            os.chmod(holder, stat.S_IRWXU)
            for f in glob.glob(os.path.join(holder, "*", "*")):
                os.remove(f)
            for sub in glob.glob(os.path.join(holder, "*")):
                os.rmdir(sub)
            os.rmdir(holder)

    def test_rm_reports_permission_error_but_stays_quiet_on_missing(self):
        with captured_stderr() as err:
            scout._rm(os.path.join(tempfile.gettempdir(), "scout_definitely_absent_file"))
        self.assertEqual(err.getvalue(), "", "отсутствие файла — штатная ситуация, лог не нужен")

        holder = tempfile.mkdtemp(prefix="scout_rm_")
        victim = os.path.join(holder, "victim")
        open(victim, "w").close()
        os.chmod(holder, stat.S_IRUSR | stat.S_IXUSR)  # удалять внутри нельзя
        try:
            with captured_stderr() as err:
                ok = scout._rm(victim)
            if os.geteuid() != 0:  # root игнорирует права — проверка осмысленна не под root
                self.assertFalse(ok)
                self.assertIn("ОШИБКА", err.getvalue())
        finally:
            os.chmod(holder, stat.S_IRWXU)
            if os.path.exists(victim):  # под root _rm уже удалил файл
                os.remove(victim)
            os.rmdir(holder)


class TestContractUnchanged(unittest.TestCase):
    """R6: вызовы из CLI и внешних обёрток не ломаются."""

    def test_signatures_unchanged(self):
        import inspect
        self.assertEqual(list(inspect.signature(scout.transcribe).parameters),
                         ["reel", "groq_key", "dl_tries"])
        self.assertEqual(list(inspect.signature(scout.transcribe_yt).parameters),
                         ["video", "groq_key", "dl_tries"])

    def test_no_videourl_returns_none_without_touching_disk(self):
        with mock.patch.object(scout.subprocess, "run") as run:
            self.assertIsNone(scout.transcribe({"videoUrl": ""}, "k"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
