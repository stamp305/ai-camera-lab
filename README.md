# ai-camera-lab

ที่ทดลองฟีเจอร์ดูแลสาขาจากระยะไกล แยกจากรีโปบริษัท (Bitbucket) — ทำให้ใช้ได้ที่นี่ก่อน แล้วค่อยย้ายเข้ารีโปบริษัททีหลัง

| เฟส | เรื่อง | ฝั่ง client | ฝั่งแอดมิน | สถานะ |
|---|---|---|---|---|
| 1 | ฐาน: mock admin + client | `client/` | `admin/` | ✅ |
| 2 | log เข้ารหัส → ดึงมาที่แอดมิน → ถอดบนเซิร์ฟเวอร์ → แสดงให้ผู้ขอดู | `log_seal.py` (เปิด) + `log_agent.py` | `log_pull_service` + `log_unseal` + `audit_log`, หน้า Log Dashboard | ✅ ทดสอบในเครื่อง |
| 3 | รีโมตเข้าเว็บของสาขา (ท่อ TLS) | `remote_agent.py` | `remote_proxy` / `remote_api` | ⏳ |
| 4 | ดูกล้องสด (Live Display ผ่านท่อรีโมต, MJPEG) | หน้า Live Display | หน้า live | ⏳ |
| 5 | สั่งอัปเดต client ผ่านแอดมิน + service เข้ารหัส (DLC) | มีแล้ว | มีแล้ว (mock admin) | ทดสอบร่วม |

## มาจากไหน

| โฟลเดอร์ | ต้นทาง |
|---|---|
| `admin/` | รีโปส่วนตัว `stamp305/bulid` branch `mock-admin` @ `0bd6660` (เฉพาะไฟล์ที่ track) |
| `client/` | รีโปบริษัท `develop128/ai-camera-client` @ `d0c90af` (เฉพาะไฟล์ที่ track) |
| เฟส 2 ต้นแบบ | `~/Documents/ai-camera-pull-logs` (ก.ย. 2026): `client/log_agent.py`, `admin/.../log_pull_service.py`, `api_logs.py` |
| เฟส 3–4 ต้นแบบ | `~/Documents/ai-camera-remote-test` (ก.ย. 2026): `backend/remote_proxy.py`, `remote_api.py`, `remote_protocol.py`, `frontend/.../remote_tunnel.py`, `blueprints/remote.py` |

## กติกาความปลอดภัย (secure by design / PDPA)

- **ความลับไม่ขึ้น git**: `_secrets.py`, keyguard, กุญแจ DLC, private key ถอด log, `.env`, ฐานข้อมูล — ดู `.gitignore`.
  สำเนา client ใน lab นี้ **ถอดรหัสผ่านฐานข้อมูลที่ฝังในโค้ดออกแล้ว** (`generate_secure_env.py`,
  `tools/upgrade_branch.py`, `tools/mariadb_setup.sh`) ให้อ่านจาก env:
  `AICAM_DB_PASSWORD`, `AICAM_DB_ROOT_PASSWORD`, `AICAM_DB_VIEWER_PASSWORD`, `AICAM_LEGACY_ROOT_PASSWORD`, `MARIADB_ROOT_PW`
- **log**: client มีแค่ public key (เข้ารหัสได้ ถอดไม่ได้). private key อยู่บนเซิร์ฟเวอร์แอดมินเท่านั้น
  นอก repo, สิทธิ์ไฟล์ 600, ชี้ด้วย env `AICAM_LOG_SEAL_KEY`. ถอดแล้วส่งให้ผู้ขอดูอย่างเดียว ไม่เขียน plaintext ลงดิสก์.
  ทุกครั้งที่ดู → audit log (ใคร / สาขา / ไฟล์ / เวลา)
- **ท่อรีโมต (65434)**: ต้นแบบเดิมเป็น TCP เปล่า — ใน lab ต้องห่อ TLS ก่อนส่งภาพหรือ log ผ่านมัน.
  token ใช้ครั้งเดียว 60 วิ, เซสชัน 30 นาที, client ยิงได้แค่ loopback + พอร์ตของแอป
- **กล้องสด = ข้อมูลส่วนบุคคล (ภาพลูกค้า)**: ดูได้เฉพาะบัญชีที่มีสิทธิ์, แอดมินไม่บันทึกภาพ, ตัดอัตโนมัติเมื่อหมดเวลา,
  audit log ใคร/กล้องไหน/เริ่ม–จบ
- **เรื่องที่รู้แล้วในโค้ด client ที่ยังไม่แก้ (รีโปบริษัท)**: รหัส DB เดียวกันทุกสาขา (คอมเมนต์ใน `upgrade_branch.py`);
  บัญชี UI เริ่มต้น `admin`/`admin` (`data_service/utils/init_account_config.py`)

## ตั้งค่าเซิร์ฟเวอร์แอดมินสำหรับเฟส 2

```bash
export AICAM_LOG_SEAL_KEY=/path/outside/repo/log_seal_private.pem   # chmod 600 ไม่งั้นไม่โหลด
export AI_CAMERA_ADMIN_AUDIT_LOG=/var/log/ai-camera/audit.jsonl     # ไม่ตั้ง = admin/backend/log/audit.jsonl
```

- ไฟล์ที่ดึงมาอยู่ใต้ `admin/backend/client_logs/<session>/...` **ยังเข้ารหัสอยู่เสมอ** — ถอดในหน่วยความจำตอนกดดู/ดาวน์โหลดเท่านั้น
- ทุกการ list / pull / ดู / ดาวน์โหลด ลง audit log (ผู้ดู = IP จนกว่าจะมี login รายคน — แก้ที่ `services/audit_log.actor()` ที่เดียว);
  เขียน audit ไม่ได้ = ไม่ให้ดู
- ข้อจำกัด: client ส่งเฉพาะ `log/`, `logs/` ใต้ `~/Library/Application Support/AICamera` — สาขาแบบ git clone ที่ service เขียน log ไว้ในโฟลเดอร์รีโป ยังดึงไม่ได้

## เตรียมรันในเครื่อง

`client/` ใช้ `python_env`, `bin` (MariaDB), `db_support`, `_secrets.py` จากเครื่อง dev — ไม่อยู่ใน git, ลิงก์เอาเอง:

```bash
cd client
ln -s ~/Documents/ai-camera-client-test/python_env python_env
ln -s ~/Documents/ai-camera-client-test/bin bin
ln -s ~/Documents/ai-camera-client-test/db_support db_support
cp ~/Documents/ai-camera-client-git/_secrets.py . && chmod 600 _secrets.py
```

`admin/` รันตาม `admin/DEPLOYMENT.md` / `admin/run_mock_admin.sh`
