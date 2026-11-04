#!/usr/bin/env python3
"""카카오톡 지정 대화방 수집기 (Claude Code 스킬용).

- rooms.json 허용 목록에 등록한 방만 읽는다.
- kakaocli는 읽기 전용 명령(chats, messages)만 호출한다. 발송·로그인 등은 막혀 있다.
- 저장 전 주민등록번호·카드번호·휴대폰번호·계좌번호를 가린다.
- 지난번에 가져온 지점을 기억해 새 대화만 덧붙인다.

사용:
  kakao_collect.py candidates [--days 90]       최근 N일 안에 대화가 있는 방 목록(대화 내용 없음)
  kakao_collect.py collect [--room 이름]         허용 목록 방의 새 대화를 기록 파일에 덧붙임
  kakao_collect.py mask-file <내보낸.txt> [--name 이름]
                                                카카오톡 「대화 내보내기」 파일을 가림 처리해 저장
                                                (Windows 등 kakaocli를 쓸 수 없는 환경용)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOMS = os.path.join(SKILL_DIR, "rooms.json")
STATE = os.path.join(SKILL_DIR, "state.json")
DEFAULT_DEST = os.path.expanduser("~/카톡기록")
KST = timezone(timedelta(hours=9))
ALLOWED_SUBCOMMANDS = {"chats", "messages"}


def kakaocli_path():
    local = os.path.expanduser("~/.local/bin/kakaocli")
    if os.path.exists(local):
        return local
    found = shutil.which("kakaocli")
    if not found:
        sys.exit("kakaocli가 설치되어 있지 않습니다. README의 「설치」 절을 따라 주세요.")
    return found


def run_cli(args):
    if args[0] not in ALLOWED_SUBCOMMANDS:
        sys.exit(f"허용되지 않은 kakaocli 명령: {args[0]}")
    out = subprocess.run([kakaocli_path(), *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"kakaocli 오류: {out.stderr.strip()}")
    return json.loads(out.stdout)


# ---- 민감정보 가림 ----
RRN = re.compile(r"(?<!\d)\d{6}\s?-\s?[1-4]\d{6}(?!\d)")
CARD = re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)")
PHONE = re.compile(r"(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)")
NUMBERISH = re.compile(r"(?<![\d-])\d[\d-]{8,}\d(?![\d-])")


def mask(text):
    text = RRN.sub("[주민번호]", text)
    text = CARD.sub("[카드번호]", text)
    text = PHONE.sub("[휴대폰번호]", text)

    def account(m):
        s = m.group(0)
        digits = sum(c.isdigit() for c in s)
        if re.fullmatch(r"(19|20)\d{2}-\d{1,2}-\d{1,2}", s):  # 날짜
            return s
        if re.fullmatch(r"\d{3}-\d{2}-\d{5}", s):  # 사업자등록번호는 공개 정보
            return s
        return "[계좌번호]" if digits >= 10 else s

    return NUMBERISH.sub(account, text)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def candidates(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows, offset = [], 0
    while True:
        page = run_cli(["chats", "--page", "--contacted", "--limit", "200", "--offset", str(offset)])
        rows += page["rows"]
        if page.get("next_offset") is None:
            break
        offset = page["next_offset"]
    registered = {str(r["chat_id"]) for r in load(ROOMS, {"rooms": []})["rooms"]}
    for r in rows:
        last = r.get("last_message_at")
        if not last or datetime.fromisoformat(last.replace("Z", "+00:00")) < cutoff:
            continue
        if r["kind"].startswith("open"):  # 오픈채팅은 후보에서도 제외
            continue
        mark = "*" if r["chat_id"] in registered else " "
        when = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d")
        print(f"{mark} [{r['chat_id']}] {r['display_name']} | {r['kind']} {r['member_count']}명 | 마지막 {when} | 내 메시지 {r['sent_message_count']}/{r['message_count']}")


def fetch(chat_id, start):
    days = max(1, (datetime.now(KST) - start).days + 1)
    rows, offset = [], 0
    while True:
        page = run_cli(["messages", "--chat-id", str(chat_id), "--since", f"{days}d",
                        "--page", "--limit", "200", "--offset", str(offset)])
        rows += page["rows"]
        if page.get("next_offset") is None:
            break
        offset = page["next_offset"]
    return rows


def render(msg):
    ts = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00")).astimezone(KST)
    who = "나" if msg.get("is_from_me") else msg.get("sender", "상대")
    text = msg.get("text")
    if not text:
        text = f"[{msg.get('message_kind', '첨부')}]"
    return ts, f"- {ts:%H:%M} **{who}**: {mask(text).replace(chr(10), ' / ')}"


def collect(only):
    rooms = load(ROOMS, {"rooms": []})["rooms"]
    if not rooms:
        sys.exit("rooms.json에 등록된 방이 없습니다. rooms.example.json을 참고해 먼저 등록해 주세요.")
    state = load(STATE, {})
    if only:
        rooms = [r for r in rooms if r["name"] == only]
        if not rooms:
            sys.exit(f"허용 목록에 없는 방: {only}")
    for room in rooms:
        cid = str(room["chat_id"])
        st = state.get(cid, {})
        seen_ts = st.get("last_timestamp")
        start = datetime.fromisoformat(seen_ts) if seen_ts else datetime.fromisoformat(room["start_date"]).replace(tzinfo=KST)
        last_ids = set(st.get("last_ids", []))
        msgs = [m for m in fetch(cid, start)
                if datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")) > start
                and m["log_id"] not in last_ids]
        # 입장·퇴장·삭제 같은 시스템 알림 제외
        msgs = [m for m in msgs if not (m.get("text") or "").lstrip().startswith('{"') or '"feedType"' not in m["text"]]
        msgs.sort(key=lambda m: (m["timestamp"], int(m["log_id"])))
        print(f"\n### {room['name']} — 새 메시지 {len(msgs)}건")
        if room.get("focus"):
            print(f"(이 방에서 볼 것: {room['focus']})")
        if not msgs:
            continue
        dest_dir = os.path.expanduser(room.get("dest_dir") or DEFAULT_DEST)
        dest = os.path.join(dest_dir, f"카톡_{room['name']}.md")
        os.makedirs(dest_dir, exist_ok=True)
        lines, day = [], None
        if not os.path.exists(dest):
            lines.append(f"# 카카오톡 기록 — {room['name']}\n\n> 카톡수집 스킬이 자동으로 덧붙입니다. 민감정보는 가려져 있습니다.\n")
        for m in msgs:
            ts, line = render(m)
            if ts.date() != day:
                day = ts.date()
                lines.append(f"\n## {day:%Y-%m-%d}")
            lines.append(line)
        block = "\n".join(lines) + "\n"
        with open(dest, "a", encoding="utf-8") as f:
            f.write(block)
        os.chmod(dest, 0o600)
        print(block)
        last = msgs[-1]["timestamp"]
        state[cid] = {
            "last_timestamp": datetime.fromisoformat(last.replace("Z", "+00:00")).isoformat(),
            "last_ids": [m["log_id"] for m in msgs if m["timestamp"] == last],
        }
        save(STATE, state)


def mask_file(src, name, dest_dir):
    raw = open(src, "rb").read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit("파일 글자 인코딩을 읽지 못했습니다.")
    name = name or os.path.splitext(os.path.basename(src))[0]
    dest_dir = os.path.expanduser(dest_dir or DEFAULT_DEST)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"카톡_{name}_내보내기_{datetime.now(KST):%Y%m%d}.md")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(f"# 카카오톡 내보내기 — {name}\n\n> 민감정보를 가린 사본입니다. 원본 txt는 확인 후 지워 주세요.\n\n")
        f.write(mask(text))
    os.chmod(dest, 0o600)
    print(f"저장: {dest}")
    print(f"원본 파일: {src} (가림 처리 전 원본이므로 Claude가 열지 않습니다. 확인 후 직접 삭제해 주세요.)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--days", type=int, default=90)
    k = sub.add_parser("collect")
    k.add_argument("--room")
    m = sub.add_parser("mask-file")
    m.add_argument("src")
    m.add_argument("--name")
    m.add_argument("--dest-dir")
    a = p.parse_args()
    if a.cmd == "candidates":
        candidates(a.days)
    elif a.cmd == "collect":
        collect(a.room)
    else:
        mask_file(a.src, a.name, a.dest_dir)


if __name__ == "__main__":
    main()
