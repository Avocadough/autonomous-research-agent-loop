# Agent-loop controller (minimal, local)

`TASK.md -> planner -> implementation -> deterministic verifier
-> independent read-only reviewer -> repair (max 2) -> final state`

## สิ่งที่มันทำ (ย่อ)

- เป็น state machine ชัดเจน 10 สถานะ:
  `PENDING PLANNING IMPLEMENTING VERIFYING REVIEWING REPAIRING
  DONE BLOCKED NEEDS_DECISION BUDGET_EXCEEDED`
- เรียก OpenCode ผ่าน CLI ปัจจุบัน (`opencode run --format json
  --agent ... --file ... --title ... "<prompt>"`, ทดสอบกับ opencode v2.0.10)
  — **หนึ่ง phase = หนึ่ง fresh session เสมอ** ไม่มีการ `--continue` /
  `--session` ข้าม phase reviewer จึงได้ fresh context จริง
- planner ใช้ agent `plan` (read-only), implement/repair ใช้ `build`,
  reviewer ใช้ `plan` ใน fresh session (custom subagent ถูก provider tier นี้
  refuse ด้วย 403 จึงใช้ built-in แทน — ไม่เพิ่ม agent ใหม่)
- **LLM prose ไม่ใช่ control evidence**: controller ไม่ดักคำอย่าง credentials/
  scope/methodology ใน text อิสระ; escalation มาจาก system evidence เท่านั้น
  (git delta เทียบ scope, diff inspection, exit codes, transport errors, repair
  counter, failure signature) + การ **พยายามทำ forbidden action จริง**
  (tool_use events ใน session stream) + verdict token เดียวของ reviewer:
  `REVIEW_VERDICT: PASS|CHANGES_REQUIRED|BLOCKED` (ขาด/ผิด format = NEEDS_DECISION)
- provider rate-limit ไม่ใช่ implementation failure: retry แยกงบ
  (`transport_max_retries`, backoff ตาม `transport_backoff_s` หรือค่าที่ provider
  แนะนำ) ไม่กิน repair budget; หมดงบแล้วได้ `BLOCKED` หมวด `provider-rate-limited`
- ทุก LLM call ใช้ model ชัดเจนจาก config (`model: provider/model`,
  optional `model_variant`) บันทึก provider/model/variant/session/exit code
  ลง transcript; live run ไม่มี model = refuse ทันที ไม่ fallback เงียบ
- verifier อยู่นอก LLM: รันคำสั่งจาก section `# Verification` ใน TASK.md
  (fallback: `default_verification` ใน `.agent-loop/config.json`) แล้วบันทึก
  exit code + output ทุกคำสั่ง
- ซ่อมอัตโนมัติสูงสุด **2 ครั้ง**; เกินแล้วได้ `BUDGET_EXCEEDED`
- เจอเงื่อนไขข้อ 5 (acceptance/methodology เปลี่ยน, ต้องใช้ credentials/
  production, ล้มเหลวซ้ำโดยไม่มีหลักฐานใหม่, งานเกิน scope) จะหยุดที่
  `NEEDS_DECISION`
- คำสั่งต้องห้าม (`git push`, deploy, secrets, ลดทอน test) จะถูก **refuse
  ก่อนรัน** และไม่ถูก execute เด็ดขาด
- หลักฐานทั้งหมดอยู่ที่ `.agent-loop/runs/<run-id>/`:
  `state.json plan.md verification.json review.md result.md`
  (+ `diff.patch transcript.jsonl review_meta.json`)

## โครงไฟล์

- `tools/agent_loop/controller.py` — state machine + prompts + guardrails
- `tools/agent_loop/verifier.py` — deterministic verifier
- `tools/agent_loop/opencode_runner.py` — `opencode run` wrapper + dry-run runner
- `tools/agent_loop/cli.py` — entry point
- `tools/test_agent_loop.py` — tests ของ controller
- `.agent-loop/config.json` — repository configuration ของ verifier/loop

## วิธีรัน 1 task (PowerShell, ที่ repo root)

### 1) Dry-run ปลอดภัย (ไม่เรียก LLM, ไม่แก้ไฟล์, verifier รันเฉพาะคำสั่ง allowlist)

```powershell
py -3.12 -X utf8 tools/agent_loop/cli.py --task TASK.md --run-id demo01 --dry-run
```

TASK.md ฉบับ dry-run ควรมี `# Verification` เป็นคำสั่งอ่านอย่างเดียว เช่น:

```markdown
# Verification

- py -3.12 --version
- git status --short
```

(ต้องอยู่ใน `dry_run_allowlist` ของ `.agent-loop/config.json`
ไม่งั้น verifier จะ refuse)

### 2) รันจริง (เรียก `opencode run` ทีละ phase, ต้องมี model/auth พร้อม)

```powershell
py -3.12 -X utf8 tools/agent_loop/cli.py --task TASK.md --run-id task01
```

ออปชัน: `--max-repairs 2` (default ตาม config), `--model <provider/model>`,
`--repo-root <path>`, `--run-id` (default เป็น timestamp UTC)
### 3) ดูผล

```powershell
Get-ChildItem .agent-loop/runs/<run-id>/
Get-Content .agent-loop/runs/<run-id>/result.md
```

exit code: `0`=DONE, `1`=BLOCKED, `2`=NEEDS_DECISION, `3`=BUDGET_EXCEEDED

## Review modes (V1)

- **STRUCTURED_REVIEW** — ใช้เมื่อ OpenCode installation มี native JSON-Schema
  structured output แล้ว model ส่ง verdict ที่ validate ได้ (`PASS /
  CHANGES_REQUIRED / BLOCKED`) โดย `PASS` → DONE, `CHANGES_REQUIRED` → repair
  (งบซ่อม 2 ครั้งเท่าเดิม), `BLOCKED` → BLOCKED
- **ADVISORY_REVIEW** — โหมดที่ installation นี้ใช้จริง (probe แล้ว:
  `opencode run` ไม่มี schema flag, SDK prompt API ไม่มี schema field)
  reviewer ยังรัน fresh session แบบ read-only และเก็บ prose ไว้เป็นหลักฐาน
  แต่ prose ไม่คุม state; DONE ต้องผ่าน hard gates ดีเทอร์มินิสติกครบ
  (plan/executor สำเร็จ, planning delta ศูนย์, ไม่มี forbidden action,
  อยู่ใน scope, ไม่ weaken tests, verifier PASS, งบซ่อมไม่เกิน)
  `result.md` จะระบุ `review_mode = ADVISORY_REVIEW` ไว้ชัดเจน

## Safety limitations

- ADVISORY_REVIEW ใช้ได้เฉพาะงาน local/branch ที่ย้อนกลับได้เท่านั้น
  ห้ามใช้ authorize deploy, production changes, แตะ secrets, หรือ real-money actions
- loop นี้ไม่ `git push`/deploy/แตะ secrets ให้เอง; คำสั่งต้องห้ามถูก refuse
  ก่อนรัน (verifier + tool-attempt detection)
- scope-check เทียบชื่อไฟล์แบบ explicit (ไม่ใช่ prefix); ระบุไฟล์เป็นรายไฟล์
- test-weakening detection เป็น heuristic บน git diff ไม่ใช่ proof สมบูรณ์

## รัน tests ของ controller

```powershell
py -3.12 -X utf8 -m unittest discover -s tools -p "test_agent_loop.py"
```

tests ไม่เรียก LLM/network; มีเพียง 2 คำสั่ง `py -c` จริงเพื่อพิสูจน์ว่า
verifier บันทึก exit code ตรง

## ข้อจำกัดที่รู้แล้ว

- `opencode run --format json` ไม่มี schema รับประกัน: runner parse แบบ
  best-effort แต่เก็บ raw output ไว้ใน transcript เสมอ
- scope-check เทียบชื่อไฟล์กับ section Allowed Scope แบบตรงตัว;
  TASK.md template ที่ยังว่างจะ enforce ไม่ได้ (ข้าม)
- test-weakening detection เป็น heuristic บน git diff (assert/test ที่ถูกลบ)
  ไม่ใช่ proof สมบูรณ์ — กรณี PASS พร้อม diff น่าสงสัยจะ escalate เป็น
  NEEDS_DECISION ให้คนตัดสิน
