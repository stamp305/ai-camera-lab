# วิธีใช้ `tools/mariadb_setup.sh`

สคริปต์ติดตั้ง/จัดการ MariaDB **ท้องถิ่น** ของเครื่องสาขา AI Camera — ฐานข้อมูลนี้
อยู่บนเครื่องร้านเท่านั้น **แอดมินกลาง (app.advice.th) ไม่มีทางเข้าถึงได้**
(remote access ถูกปิดสนิททั้งระดับ account และระดับ network bind)

## 1. แก้ค่าตั้งต้นก่อนใช้

เปิด `tools/mariadb_setup.sh` แก้ 4 ตัวแปรบนสุดของไฟล์:

```bash
DB_NAME="__CHANGE_ME_DB_NAME__"        # ชื่อฐานข้อมูลของแอป (เช่น ai_camera)
APP_USER="__CHANGE_ME_APP_USER__"      # username ของแอป
APP_USER_PW="__CHANGE_ME_APP_PW__"     # รหัสผ่านของแอป
ROOT_PW="__CHANGE_ME_ROOT_PW__"        # รหัสผ่าน root — ผู้ติดตั้งตั้งเอง
```

> ⚠️ ห้าม commit ค่าจริงกลับเข้า git — ไฟล์นี้ถูก push ไปทุกสาขา

## 2. รัน

```bash
bash tools/mariadb_setup.sh            # ติดตั้ง/จัดการให้ครบ + ตรวจผล
bash tools/mariadb_setup.sh --check    # ตรวจอย่างเดียว ไม่แก้อะไร
bash tools/mariadb_setup.sh --stop     # หยุด daemon ที่สคริปต์สตาร์ทเอง
```

รันซ้ำได้เสมอ (idempotent) — สิ่งที่มีอยู่แล้วจะถูก "ทับให้ตรง" ไม่ใช่สร้างซ้ำ

## 3. สิ่งที่สคริปต์ทำ

| ข้อ | ทำอะไร |
|---|---|
| 1 | หา binary `mariadbd` (bundled ใน repo `bin/` → Homebrew → PATH) |
| 2 | init datadir + สตาร์ท server ด้วย `--bind-address=127.0.0.1 --skip-name-resolve` |
| 3 | ตั้งรหัสผ่าน `root@localhost` เป็น `ROOT_PW` (ผู้ติดตั้งเป็นคนกำหนด) |
| 4 | สร้าง DB ตาม `DB_NAME` + user แอป (`localhost`, `127.0.0.1`) ให้สิทธิ์ **เฉพาะ** `` `{DB_NAME}`.* `` |
| 5 | ลบ account แบบ remote (`user@'%'`) และ anonymous users ทิ้ง |
| 6 | ตรวจว่า port 3306 ฟังที่ loopback เท่านั้น + ไม่เหลือ account remote |

## 4. sync กับ `.env` ของ repo

ให้แอปใช้ user สิทธิ์ต่ำ (ไม่ใช่ root) — แก้ `.env` ที่ root ของ repo:

```
DB_USER=<APP_USER>
DB_PASSWORD=<APP_USER_PW>
DB_NAME=<DB_NAME>
DB_HOST=localhost
DB_ROOT_USER=root
DB_ROOT_PASSWORD=<ROOT_PW>
```

ตอน boot `client_logic.py:ensure_database()` จะใช้ `DB_ROOT_*` เฉพาะตอนสร้าง
table ครั้งแรก และใช้ `DB_USER` ปกติในการทำงาน — ตัว `create_user()` เองก็
สร้างเฉพาะ host `localhost`/`127.0.0.1` และจะ DROP `user@'%'` ทิ้งถ้าเจอจาก
การติดตั้งเก่า

## 5. ถ้าใช้ MariaDB ของระบบ (brew service)

สคริปต์จะใช้ server ที่รันอยู่แล้วแทนการสตาร์ทเอง แต่ต้องมั่นใจว่า bind
loopback — ตรวจด้วยโหมด `--check` ถ้า ❌ ให้แก้ my.cnf:

```ini
# /opt/homebrew/etc/my.cnf
[mysqld]
bind-address=127.0.0.1
skip-name-resolve
```

แล้ว `brew services restart mariadb`
