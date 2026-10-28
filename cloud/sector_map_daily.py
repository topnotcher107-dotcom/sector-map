"""매일 20:00 업종 등락률 맵 생성 → Sonnet이 장 분석 작성·Artifact 발행·카톡 전송.
데이터 수집·HTML 생성은 파이썬(토큰 0), Claude는 하루 1회 호출. 거래일이 아니면(당일 체결 없음) 건너뜀.
  python sector_map_daily.py             # 정상 실행
  python sector_map_daily.py --no-claude # HTML만 생성(테스트)
  python sector_map_daily.py --force     # 휴장/중복 실행 체크 무시
"""
import json, logging, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "out"
H = {"User-Agent": "Mozilla/5.0"}
API = "https://m.stock.naver.com/api"
log = logging.getLogger("sector")


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def get(url):
    for _ in range(3):
        try:
            return requests.get(url, headers=H, timeout=20).json()
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"요청 실패: {url}")


def stocks_of(no):
    out, p = [], 1
    while True:
        d = get(f"{API}/stocks/industry/{no}?page={p}&pageSize=100")["stocks"]
        out += d
        if len(d) < 100:
            return out
        p += 1


def per_pbr(code):
    try:
        m = {x["code"]: x["value"] for x in get(f"{API}/stock/{code}/integration")["totalInfos"]}
        f = lambda v: float(v.replace("배", "").replace(",", "")) if v and v not in ("N/A", "-") else None
        return code, f(m.get("per")), f(m.get("pbr"))
    except Exception:
        return code, None, None


def collect():
    g = [s for s in get(f"{API}/stocks/industry?page=1&pageSize=100")["groups"] if s["name"] != "기타"]
    for s in g:
        s["rate"] = float(s["changeRate"])
    g.sort(key=lambda s: -s["rate"])
    sel = {"up": g[:10], "down": g[::-1][:10]}
    data, trade_date = {}, None
    for k, lst in sel.items():
        sg = -1 if k == "up" else 1  # 하락 업종은 낙폭 큰 종목부터
        for s in lst:
            st = stocks_of(s["no"])
            trade_date = trade_date or (st[0].get("localTradedAt", "")[:10] if st else None)
            rows = [dict(n=x["stockName"], c=x["itemCode"], p=x["closePrice"], r=float(x["fluctuationsRatio"]),
                         m=float(x.get("marketValueRaw") or 0)) for x in st]
            for r in rows:
                r["pm"] = r["m"] / (1 + r["r"] / 100)
            tot = sum(r["pm"] for r in rows) or 1
            for r in rows:
                r["k"] = r["pm"] / tot * r["r"]
            s["cap"], s["cnt"] = sum(r["m"] for r in rows) / 1e12, len(rows)
            a = sorted(rows, key=lambda r: sg * r["r"])[:20]
            b = sorted(rows, key=lambda r: sg * r["k"])[:20]
            codes = {r["c"] for r in a + b}
            with ThreadPoolExecutor(8) as ex:
                mp = {c: (x, y) for c, x, y in ex.map(per_pbr, codes)}
            for r in a + b:
                r["per"], r["pbr"] = mp[r["c"]]
            data[s["name"]] = {"a": a, "b": b}
    secs = {k: [dict(name=s["name"], rate=s["rate"], cap=s["cap"], n=s["cnt"], up=s["riseCount"],
                     down=s["fallCount"]) for s in l] for k, l in sel.items()}
    return secs, data, trade_date


def summary(secs, data):
    L = []
    for k, label in (("up", "상승 상위"), ("down", "하락 상위")):
        L.append(f"[{label} 업종] " + ", ".join(
            f"{s['name']} {s['rate']:+.2f}%(시총 {s['cap']:.1f}조, 상승{s['up']}·하락{s['down']}종목)" for s in secs[k]))
    for s in secs["up"][:3] + secs["down"][:2]:
        b = data[s["name"]]["b"][:3]
        L.append(f"{s['name']} 기여도 상위: " + ", ".join(f"{r['n']} {r['r']:+.1f}%(기여 {r['k']:+.2f}%p)" for r in b))
    return "\n".join(L)


def build_html(secs, data, date, analysis=""):
    t = (BASE / "template.html").read_text(encoding="utf-8")
    t = t.replace("{{DATE}}", date).replace("{{ANALYSIS}}", analysis)
    t = t.replace("/*SECS*/", json.dumps(secs, ensure_ascii=False)).replace("/*STOCKS*/", json.dumps(data, ensure_ascii=False))
    OUT_DIR.mkdir(exist_ok=True)
    p = OUT_DIR / "sector_map_latest.html"
    p.write_text(t, encoding="utf-8")
    (OUT_DIR / f"sector_map_{date}.html").write_text(t, encoding="utf-8")
    return p


def find_claude():
    for name in ("claude.exe", "claude.cmd"):
        if shutil.which(name):
            return shutil.which(name)
    fb = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    if fb.exists():
        return str(fb)
    raise RuntimeError("claude CLI를 찾을 수 없습니다")


DENY_FILE = BASE / "mcp_deny.json"  # 카카오 외 커넥터 도구 제외 목록 (토큰 절약)

ANALYZE_PROMPT = """오늘({date}) 한국 주식 장 마감 업종 데이터다.
{summary}

아래 JSON만 출력하라(코드블록·설명 금지).
{{"analysis": "한국어 장 분석 HTML. <p>와 <b>만 사용, 3~5문장: 시장 주도 업종과 이유(기여도 큰 종목), 약세 업종 특징, 내일 볼 포인트. 데이터에 없는 사실은 지어내지 마라.",
 "kakao": "카톡용 한 줄 요약(60자 이내, 이모지 가능)"}}"""


def claude_cmd(cfg, extra, model=None):
    return [find_claude(), "-p", "--model", model or cfg["claude_model"], "--disable-slash-commands",
            "--no-session-persistence", *extra]


def usage_tokens(result):
    u = result.get("usage", {})
    return sum((u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def analyze(cfg, summ, date):
    """도구·커넥터 없이 텍스트만 생성 (Sonnet 1회)."""
    cmd = claude_cmd(cfg, ["--system-prompt", "너는 한국 주식 시장 요약 도우미다. 요청한 JSON만 출력한다.",
                           "--tools", "", "--strict-mcp-config", "--output-format", "json"])
    p = subprocess.run(cmd, input=ANALYZE_PROMPT.format(date=date, summary=summ), capture_output=True, text=True,
                       encoding="utf-8", cwd=BASE, timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    res = json.loads(p.stdout)
    log.info("분석 호출: 토큰 %s", usage_tokens(res))
    m = re.search(r"\{.*\}", res.get("result", ""), re.S)
    return json.loads(m.group(0))


def publish(cfg, date):
    """out/ 의 HTML을 GitHub Pages 저장소에 push하고 공개 링크를 반환."""
    repo = Path(cfg["repo_dir"])
    shutil.copy(OUT_DIR / "sector_map_latest.html", repo / "index.html")
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    run = lambda *a: subprocess.run([git, "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8")
    run("add", "index.html")
    run("-c", "user.name=topnotcher107-dotcom", "-c", "user.email=topnotcher1007@gmail.com", "commit", "-m", f"sector map {date}")
    r = run("push")
    if r.returncode:
        raise RuntimeError(f"git push 실패: {r.stderr[:300]}")
    return cfg["pages_url"]


def send_kakao(cfg, text):
    """카카오 MCP '나에게 보내기' (stream-json, 커넥터 연결 대기)."""
    tool = cfg["kakao_mcp_tool"]
    cmd = claude_cmd(cfg, ["--system-prompt", "너는 카카오톡 메시지 전송 도우미다. 받은 메시지를 KakaotalkChat-MemoChat 도구로 그대로 보내고 짧게 답한다.",
                           "--tools", "", "--allowedTools", tool, "--max-turns", "4",
                           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                     model=cfg.get("kakao_model"))
    deny = load_json(DENY_FILE, [])
    if deny:
        cmd += ["--disallowedTools", *deny]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", cwd=BASE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(int(cfg.get("mcp_wait_sec", 15)))
    if p.poll() is not None:
        raise RuntimeError(f"claude 즉시 종료: {p.stderr.read()[:300]}")
    prompt = "아래 메시지를 한 글자도 바꾸지 말고 1회 전송하라.\n===== 메시지 =====\n" + text
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
                             ensure_ascii=False) + "\n")
    p.stdin.close()
    out, err = p.communicate(timeout=300)
    sent = errs = 0
    result = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") in ("assistant", "user"):
            for b in ev.get("message", {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == tool:
                    sent += 1
                elif isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errs += 1
        elif ev.get("type") == "result":
            result = ev
    if not result:
        raise RuntimeError(f"결과 없음: {err[:300]}")
    log.info("카톡 호출: 토큰 %s", usage_tokens(result))
    if sent - errs < 1:
        raise RuntimeError("카톡 전송 실패 (mcp_wait_sec 증가 필요할 수 있음)")


def main():
    logging.basicConfig(filename=BASE / "run.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_json(BASE / "config.json", {})
    state = load_json(BASE / "state.json", {})
    today = datetime.now().strftime("%Y-%m-%d")
    secs, data, trade_date = collect()
    if trade_date != today and "--force" not in sys.argv:
        log.info("오늘(%s) 장 데이터 없음(마지막 체결 %s) → 휴장으로 보고 건너뜀", today, trade_date)
        return
    build_html(secs, data, today)
    if "--no-claude" in sys.argv:
        (OUT_DIR / "summary.txt").write_text(summary(secs, data), encoding="utf-8")  # 클라우드 루틴용
        return
    if state.get("last_run") == today and "--force" not in sys.argv:
        log.info("오늘 이미 실행함")
        return
    try:
        res = analyze(cfg, summary(secs, data), today)
        build_html(secs, data, today, res["analysis"])
        link = publish(cfg, today)
        send_kakao(cfg, f"📊 {today} 업종맵\n{res['kakao']}\n{link}")
        state["last_run"] = today
        log.info("완료: %s", link)
    except Exception as e:
        log.error("실패: %s", e)
    (BASE / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
