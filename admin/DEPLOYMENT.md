# Deployment Guide — AI Camera Admin (Linux Server)

## 1. สิ่งที่ระบบทำได้/ไม่ได้ตอน deploy โดยยังไม่มี Mac builder

| ฟังก์ชัน | สถานะ |
|---|---|
| เว็บแอดมิน / branches / assign version | ✅ |
| force-update / broadcast push เวอร์ชันที่ compile แล้ว | ✅ |
| DLC + socket update | ✅ |
| Store เวอร์ชันจาก Bitbucket | ✅ (เก็บ plaintext + snapshot รอ builder) |
| Compile เป็น .so | ⏳ ต้องมี Mac Mini builder (client สาขาเป็น macOS) |

พอเอา Mac Mini มาและรัน `prepare_builder.sh` เสร็จ → builder เชื่อมต่อเอง
→ store ครั้งต่อไป compile ทันที และใช้ปุ่ม **Build** ในหน้า version manager
(หรือ `POST /ai-camera/api/bitbucket/version/<vid>/rebuild`) เพื่อ build
เวอร์ชันเก่าที่เก็บค้างไว้ได้

## 2. Environment variables ที่ server ต้องตั้ง

**ทำง่ายสุด:** ก๊อปไฟล์สำเร็จรูป `deploy/env.server` (ค่าใส่ครบแล้ว) ขึ้น server
เช่น `/opt/ai-camera/env.server` แล้วเลือกวิธีผูกอย่างใดอย่างหนึ่ง:

```bash
# วิธี A — systemd (แนะนำ): เพิ่มบรรทัดนี้ใน [Service] section
EnvironmentFile=/opt/ai-camera/env.server

# วิธี B — export ก่อนรันมือ:
set -a; source /opt/ai-camera/env.server; set +a
python3 app_ai_camera_backend.py ...
```

> **Mac Mini ฝั่ง builder ไม่ต้องตั้ง env เอง** — `prepare_builder.sh` ถาม
> URL + key แล้วเขียน `/etc/ai-camera/builder.env` + LaunchDaemon ให้เอง

ตารางตัวแปร (รายละเอียด):

| Variable | ค่าที่แนะนำ | หมายเหตุ |
|---|---|---|
| `AI_CAMERA_ADMIN_BUILDER_KEY` | *(ดู `.builder_key`)* | ไฟล์ `backend/.builder_key` มากับ git pull แล้ว — server ต้อง chmod 600 ครั้งเดียว |
| `AI_CAMERA_ADMIN_BACKEND_PUBLIC_URL` | `https://app.advice.th` | URL ที่ builder ใช้ยิง `/build-farm/*` — **ไม่มี** `/ai-camera` ต่อท้าย |
| `AI_CAMERA_ADMIN_FRONTEND_URL` | `https://app.advice.th` | ใช้สร้าง upload_url ของ trigger-build (`/ai-camera/api/build/upload`) |
| `AI_CAMERA_FARM_CTL` | *ไม่ต้องตั้ง* | default ชี้เครื่อง dev (/Users/puriphat.bu/…) — ถ้า path ไม่มีอยู่ระบบข้าม local farm ให้เอง (builder ทางไกลคือทางเดินหลัก) |
| `AI_CAMERA_ADMIN_FARM_TUNNEL_URL` | **ปกติไม่ต้องตั้ง** | เป็นแค่ fallback แล้ว: builder ประกาศ URL ของตัวเองมาให้ตอน connect (`builder_ready` → `artifact_url`) แอดมินใช้ค่านั้นเสมอ ตั้งค่านี้เฉพาะตอนใช้ worker เวอร์ชันเก่าที่ยังไม่ประกาศ — ถ้าตั้งทิ้งไว้แล้ว tunnel ฝั่งมินิย้าย ค่านี้จะชี้ผิดเงียบ ๆ |
| `AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS` | **ไม่ต้องตั้ง (หรือถ้าตั้งต้องระวังมาก)** | สำหรับเทส LAN เท่านั้น (ยกเว้น IP ให้ผ่าน whitelist อัตโนมัติ) — ที่เทสวันนี้ใช้ `192.168.1.80,127.0.0.1` **ต้องไม่ตั้งค่านี้บน production** ไม่งั้น IP พวกนั้นจะผ่านการตรวจสิทธิ์ได้เลย |
| `AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY` | **ไม่ต้องตั้ง (ปิด)** จนกว่าจะถึงขั้นที่ 5 ของ §7 · ตั้งใน `ai-camera-admin/frontend/.env` | เมื่อตั้งเป็น `1` สาขาที่ไม่มี Device Key จะถูกปฏิเสธทั้งทาง socket และทาง upload **ถ้าเปิดก่อนทุกสาขาจะได้ key สาขาเหล่านั้นจะหลุดทันที** |
| `AI_CAMERA_ADMIN_CYTHON_PYTHON` | *ไม่ต้องตั้ง* | ใช้เฉพาะ local compile; บน server ไม่ใช้ (compile เกิดบน Mac) |
| `AI_CAMERA_ADMIN_BITBUCKET_URL` | default ใช้ได้ | repo client-code |
| `AI_CAMERA_ADMIN_CLIENT_UPDATE_MASTER` | default ใช้ได้ | `client_update_master` |
| `OPENROUTER_API_KEY` | **ต้องมี** ใน `python_server/.env` | key ที่ backend ใช้เรียก Jev ให้ `/ai-camera/api/camera-placement` · backend อ่านจาก `python_server/.env` เอง (เฉพาะบรรทัดนี้ ทุก call จึงเปลี่ยน key ไม่ต้อง restart) ถ้า env ของ process มีค่า ใช้ค่านั้นก่อน · ไม่มี = endpoint นี้ตอบ 503 ส่วนอื่นไม่กระทบ · สาขาไม่เคยเห็น key |
| `CAMERA_PLACEMENT_MODEL` | default ใช้ได้ | `typesafe/jev-1.14` (ล็อกรุ่นไว้ เปลี่ยนเมื่อเทสรุ่นใหม่แล้ว) |
| `CAMERA_PLACEMENT_MAX_SIDE` | default ใช้ได้ | ด้านยาวสุดของรูปที่ส่งให้ Jev (280–1568, default 1568) · `498` = รูปเล็ก ถูกลง ~3 เท่า |

> ⚠️ **whitelist อยู่ใน `frontend/config.py` แบ่งเป็น 2 ชุด** (คุมโดย `frontend/gate.py`, ADR 0007):
> `OFFICE_IPS` (IP ออฟฟิศอย่างเดียว) ใช้กับหน้า admin และหน้า support (:8003), ส่วน
> `TRANSITION_IPS` (IP ออฟฟิศ + `BRANCH_IPS`) ใช้เฉพาะกับ upload ของสาขาที่ยังไม่มี Device Key
> ห้ามใส่ `127.0.0.1` ในรายการใดก็ตาม เพราะเมื่อรันหลัง nginx ทุก request จะดูเหมือนมาจาก localhost
> ผลคือทุก IP บนอินเทอร์เน็ตจะผ่าน whitelist
> **deploy จาก IP ออฟฟิศเท่านั้น** ถ้าไม่ใช่ จะเข้าหน้า admin ไม่ได้

> ทุก path ที่เคย hardcode ไว้ (build farm ctl.py, Cython interpreter)
> อ่านจาก env ก่อนเสมอ — server ไม่ต้องมี folder เหล่านั้นก็รันได้

## 3. nginx บน app.advice.th

Builder + REST ของ backend ไม่ได้อยู่ใต้ `/ai-camera/` (อันนั้นเป็น frontend)
จึงต้องเพิ่ม:

```nginx
location /build-farm/ {
    proxy_pass http://127.0.0.1:8011;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
location /socket.io/ {
    proxy_pass http://127.0.0.1:8011;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 7d;
}
```

## 4. ขั้นตอน deploy

> ⚠️ **ก่อนเริ่ม — ต้องมี commit `94d1161` ขึ้นไปในโค้ดที่ deploy** (`fix(user-crud)`)
> ดูเหตุผลใน §6 ข้อ "บั๊ก sync_branches_from_users" ด้านล่าง — deploy โค้ดที่ไม่มี fix นี้
> เสี่ยงข้อมูล branches หายทั้งตารางถ้า MySQL หลุดชั่วคราว
>
> ℹ️ `client_update_master` ไม่ใช่ submodule แล้ว (เอาออกจาก git แล้ว ดู §6) — `git pull`
> จะไม่สร้างโฟลเดอร์นี้ให้ แต่ไม่ต้องกังวล โค้ดสร้างเองอัตโนมัติตอน push เวอร์ชันแรก

1. ใส่ SSH key Bitbucket สำหรับ git pull — **ทำแล้ว**
2. `git pull` โค้ดบน server → `pip install -r requirements.txt`
   (เพิ่มบัญทึก: python-socketio สำหรับ backend)
3. วาง `backend/.builder_key` (จากเครื่อง dev, chmod 600)
4. ตั้ง env ตามตาราง §2 (systemd unit หรือ export ก่อนรัน) — **อย่าตั้ง `AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS`**
5. เพิ่ม nginx location §3 แล้ว reload
6. เปิด MySQL + import schema users/services (ของเดิม) + branches (auto-init)
7. รันด้วย systemd: ก๊อป `deploy/systemd/ai-camera-admin-{backend,frontend}.service` ไป `/etc/systemd/system/`
   → `sudo systemctl daemon-reload && sudo systemctl enable --now ai-camera-admin-backend ai-camera-admin-frontend`
   (ไฟล์ unit ตั้ง path/User ตาม production .85 — ใช้แบบนี้บน .85 ตั้งแต่ 23 ก.ย. 2026 · log อยู่ใน
   `journalctl -u ai-camera-admin-backend` · ต้องไม่มี process เดิมที่เปิดด้วย nohup ถือ port 8011/8002/65432 อยู่
   ไม่งั้น backend วน `Address already in use` · ห้าม `pkill` ตัวที่ systemd คุม — `on-failure` ไม่เปิดให้ใหม่)
8. ทดสอบ: `curl https://app.advice.th/dlc-folders` → JSON ออก = backend ผ่าน proxy

ทำแค่นี้พอ — ไม่มีขั้นตอนไหนตัดออกได้อีกแล้ว (แต่ละข้อจำเป็นคนละเรื่อง: 1-2 คือได้โค้ดมา, 3 คือ builder เซ็นได้,
4 คือ service หา config เจอ, 5 คือ client เข้าถึง backend/socket ได้, 6 คือมี DB ให้ backend ใช้, 7 คือรันจริง,
8 คือยืนยันผล)

## 5. ติดตั้ง Mac Mini Builder (ภายหลัง)

ดู `mac-mini-builder-package/DEPLOY_VALUES.txt` — URL/key พร้อมกรอก,
รัน `sudo bash scripts/prepare_builder.sh`, จบด้วย
`python3.12 scripts/test_socket_build.py`

## 6. ข้อควรระวัง

- **app.advice.th/ai-camera มีระบบ live อยู่** — backup ฐานข้อมูล+โค้ดเดิมก่อนทับ
  และเผื่อ rollback (client สาขาต่อ socket.io ผ่าน path /ai-camera/ ตลอดเวลา)
- key `.builder_key` = ความลับ ห้าม commit ขึ้น Bitbucket (อยู่ใน .gitignore แล้ว)

### บั๊ก sync_branches_from_users (แก้แล้วใน commit `94d1161`)

อธิบายง่าย ๆ: หน้า "จัดการเวอร์ชัน" ทุกครั้งที่เปิด จะไปอ่านตาราง `users` (MySQL) มาเทียบ
แล้ว **ลบ branch ไหนก็ตามที่ไม่มีชื่ออยู่ในตาราง `users` ทิ้ง** เพื่อ sync สองตารางให้ตรงกัน — ปกติทำงานถูก

ปัญหาที่เจอ (ก่อนแก้): ถ้า MySQL หลุดชั่วคราว (เช่น server รีสตาร์ท, connection pool หมด, network กระตุก)
โค้ดเดิมจะ**อ่านตาราง users ไม่ได้แล้วคิดว่า "ไม่มี user เลย" (list ว่างเปล่า)** แทนที่จะรู้ว่า
"อ่านไม่ได้" กับ "อ่านได้แต่ไม่มีจริง ๆ" เป็นคนละเรื่องกัน — พอเห็น list ว่าง มันจะสรุปว่า
**branch ทุกตัวไม่มีอยู่ในระบบแล้ว แล้วลบทิ้งหมดทั้งตาราง** (ประวัติ assigned_version ของทุกสาขาหายหมด)

เจอเหตุการณ์จริงตอนเทสวันนี้ (แก้บั๊กเล็กอีกจุดหนึ่งไปก่อน แล้วบั๊กนี้โผล่ตามมาทันที — MySQL
ต่อไม่ติดแค่ตอนนั้น ตารางก็ถูกลบว่างทันที) แก้แล้วใน `94d1161` ให้แยกสองเคสออกจากกัน:
"อ่าน DB ไม่ได้" จะไม่ลบอะไรเลย (ปลอดภัยเหมือนพฤติกรรมเดิมของระบบ), มีแค่ "อ่านได้แต่ไม่มี user นั้นแล้วจริง ๆ"
เท่านั้นที่จะลบ

**สรุปสิ่งที่ต้องทำ**: แค่ให้แน่ใจว่าโค้ดที่ deploy มี commit นี้รวมอยู่ด้วย (`git log` เช็คว่ามี
`fix(user-crud)` ก่อน deploy) ไม่ต้องทำอะไรเพิ่มอีก เป็นแค่การเตือนไม่ให้พลาด deploy โค้ดเก่า

### client_update_master — ตัดสินใจแล้ว: เอาออกจาก git ทั้งหมด (ไม่ใช่ submodule ต่อไป)

`backend/client_update_master` เดิมเป็น git submodule แต่ pointer ของมันขยับเป็น `-dirty`
ทุกครั้งที่ตรวจ (`4035c86-dirty`) — ไล่โค้ดเจอสาเหตุ: **โฟลเดอร์นี้คือ staging directory ที่
`branch_update_manager.py:_sync_version_to_client_update_master()` ลบเนื้อหาเดิมแล้วเขียนทับด้วย
เวอร์ชันที่กำลัง push ทุกครั้งที่มีการ force-update/auto-update ผ่าน socket.io** — ไม่ใช่ source
code ที่ dev แก้ แต่เป็นสถานะ runtime ที่ระบบสร้าง/เขียนทับเองตลอดเวลา จึง**ไม่มีทาง commit เป็น
snapshot นิ่ง ๆ ได้เลยในทางปฏิบัติ** (จะ dirty ใหม่ทันทีหลัง push ครั้งถัดไป)

**แก้แล้ว**: เอาออกจาก git index (`git rm --cached`) แล้วใส่เข้า `.gitignore` แทน (เหมือน
`backend/client_code/` ที่ gitignore ไปแล้วด้วยเหตุผลเดียวกัน — เป็น runtime cache ไม่ใช่ source)
โค้ดสร้างโฟลเดอร์นี้เองอัตโนมัติตอน push เวอร์ชันแรก (`os.makedirs(dst, exist_ok=True)`)
ไม่ต้องเตรียมอะไรเพิ่มตอน deploy — แค่ให้แน่ใจว่า process ที่รัน backend มีสิทธิ์เขียนไฟล์ในโฟลเดอร์
`backend/` ได้ตามปกติ

## 7. Device Key ของสาขา (ADR 0007)

เอกสารอ้างอิง: `docs/adr/0007-branch-access-by-device-key.md`, `.scratch/ai-camera-branch-device-key/spec.md`, `client-contract.md`

**ลำดับการ rollout (ห้ามสลับลำดับ)**
1. **เพิ่มคอลัมน์ใน DB ก่อน**
   ```sql
   ALTER TABLE users ADD COLUMN device_key_hash CHAR(64) NULL;   -- deploy/migrations/20260924_add_users_device_key_hash.sql
   SHOW COLUMNS FROM users LIKE 'device_key_hash';               -- ต้องได้ 1 แถว
   ```
   - backend จะพยายามสร้างคอลัมน์นี้ให้เองตอนเริ่มทำงาน แต่ถ้าสร้างไม่สำเร็จ จะแค่ print แล้วทำงานต่อ
   - ถ้าคอลัมน์ไม่มี `/auth/verify-device` จะตอบ 500 ระหว่างที่ switch ปิด สาขาที่ไม่มี key ยังผ่าน identify ได้ (log `allowed keyless identify`) แต่สาขาที่มี key จะต่อ socket ไม่ได้จนกว่าคอลัมน์จะมี
2. **ตรวจว่า activation code ของทุกเครื่องที่ online อยู่มีในตาราง `users`**
   ```bash
   sudo journalctl -u ai-camera-admin-frontend --since "2 days ago" | grep -o "Identified client_id=[^ ]*" | sort -u
   ```
   - เทียบผลกับ `SELECT activation_code FROM users;`
   - code ที่ไม่มีในตาราง จะถูกตัดตอน identify
3. **deploy server:** restart **backend ก่อน** แล้วค่อย restart frontend
   - ถ้า frontend ขึ้นก่อน การเรียก verify-device จะได้ 404 ระบบจะถือว่า backend ล่ม สาขาที่ไม่มี key ยังผ่านได้ (switch ปิด) แต่สาขาที่มี key จะต่อไม่ได้จน backend ขึ้น (client ลองต่อใหม่เอง)
   - ห้ามตั้ง `AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY` ตอน deploy
4. **ปล่อย Main Client เวอร์ชันใหม่** ตัวที่เก็บ Device Key และ activate เองเมื่อยังไม่มี key
   - ห้ามปล่อยก่อนขั้นที่ 3
   - ช่วงนี้**ห้าม activate ร้านใหม่ด้วย client เวอร์ชันเก่า** เพราะ key ที่ server ออกให้จะหายไป ถ้าเผลอทำ ให้เคลียร์ key ตามหัวข้อ "กู้คืน" ด้านล่าง
5. **รอให้ทุกสาขาที่ใช้งานอยู่พร้อม แล้วค่อยเปิด switch** ต้องผ่านทั้ง 3 ข้อ:
   - **ทุกสาขามี key ใน DB** ผลของคำสั่งนี้ต้องเหลือแต่เครื่องที่เลิกใช้แล้ว
     ```sql
     SELECT activation_code FROM users WHERE device_key_hash IS NULL;
     ```
   - **ทุกสาขารัน Main Client เวอร์ชันที่รองรับ Device Key แล้ว** ดูจากคอลัมน์ last_version ในหน้า version manager
     - DB บอกได้แค่ว่า Main Client เคยได้ key
     - ส่วน DLC uploader จะได้ key ก็ต่อเมื่อ Main Client เวอร์ชันนั้นส่ง env `AI_CAMERA_ACTIVATION_CODE` และ `AI_CAMERA_DEVICE_KEY` ต่อให้ DLC
   - **ไม่มี upload แบบไม่มี key เหลืออยู่** ต้องเช็คข้อนี้ด้วย เพราะเป็นตัวเดียวที่บอกได้ว่า DLC ของแต่ละสาขาส่ง key มาจริง ผลลัพธ์ต้องว่าง หรือมีแต่ IP ที่ไม่ใช่สาขาที่ใช้งานอยู่
     ```bash
     sudo journalctl -u ai-camera-admin-frontend --since "3 days ago" | grep "Keyless branch_rest request passed by IP"
     ```

   **เปิด switch** ใส่บรรทัดนี้ใน `ai-camera-admin/frontend/.env` (ไม่อยู่ใน git · frontend อ่านเฉพาะบรรทัดนี้ ตอนเริ่มทำงาน · ถ้า env ของ process ตั้งค่านี้ไว้ ค่านั้นชนะ):
   ```bash
   cd /home/ubuntu/advice/python_server/ai-camera-admin/frontend
   printf '\nAI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY=1\n' >> .env
   systemctl show ai-camera-admin-frontend -p Environment   # ต้องไม่มี AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY (ค่าใน env ของ process ชนะ .env)
   sudo systemctl restart ai-camera-admin-frontend
   ```

   หลัง restart ให้ยืนยันว่า switch เปิดแล้วจริง: `journalctl -u ai-camera-admin-frontend -n 50 | grep "Require Device Key"` ต้องเห็น `True`

   **เฝ้า log อย่างน้อย 30 นาทีหลังเปิด** ถ้ามีสาขาที่ยังใช้งานอยู่โผล่มาในผลลัพธ์ ให้ปิด switch ทันที
   ```bash
   sudo journalctl -u ai-camera-admin-frontend --since "30 min ago" \
     | grep -E "Refused (branch_rest|keyless branch socket|identify)"
   ```

   **ปิด switch**
   ```bash
   cd /home/ubuntu/advice/python_server/ai-camera-admin/frontend
   sed -i -E '/^[[:space:]]*(export[[:space:]]+)?AI_CAMERA_ADMIN_REQUIRE_DEVICE_KEY[[:space:]]*=/d' .env
   sudo systemctl restart ai-camera-admin-frontend
   ```
   แล้วเช็คว่า log ขึ้น `Require Device Key: False`
6. **ภายหลัง:** ลบ `BRANCH_IPS` ออกจาก `frontend/config.py`

**หลัง deploy ให้เช็ค**
- จำนวนสาขา online ในหน้า admin ต้องเท่ากับก่อน deploy
- `journalctl -u ai-camera-admin-frontend | grep "Refused identify"` ต้องไม่มีผลลัพธ์
- กด push Main Client 1 ครั้ง แล้วต้องไม่มี `Blocked unauthorized admin request` ใน log
  - ถ้ามี แปลว่า server เรียก frontend ของตัวเองผ่าน IP ที่ไม่ใช่ IP ออฟฟิศ ให้เพิ่ม IP นั้นใน `AI_CAMERA_ADMIN_EXTRA_ALLOWED_IPS`

**กู้คืนเมื่อสาขาทำ key หาย** (ติด 409 `device_key_exists`)
```sql
UPDATE users SET device_key_hash = NULL WHERE activation_code = '<code>';
```
- **ต้อง restart แอป AICamera ที่สาขาด้วย** (ให้ร้านปิดแล้วเปิดแอปใหม่)
  - ตาม client contract เมื่อ client ได้ 409 จะหยุดและรอ admin ไม่ลองใหม่เอง
  - พอแอปเปิดขึ้นใหม่และเห็นว่ายังไม่มี key จะ activate เองแล้วได้ key ใหม่
  - ถ้า key ที่แอปถืออยู่ถูกเคลียร์ทิ้งทั้งที่แอปยังเปิดอยู่ แอปจะได้ 401 แล้วไป activate ใหม่เอง ไม่ต้อง restart
- ถ้าสาขาติด 429 (`too_many_attempts`) อยู่ด้วย ตัวนับจะหมดอายุเองภายใน 1 ชั่วโมง ถ้ารอไม่ได้ ให้ restart frontend ซึ่งจะล้างตัวนับทั้งหมด ตัวนับนับเฉพาะการตอบ 401, 404 และ 409
- การตั้ง `is_activated = 0` ก็เปิดให้ activate ได้เหมือนกัน
- ทั้งสองวิธีไม่ทำให้ activation code, version หรือ package ของสาขาหาย

**ย้อนกลับ:** checkout `ai-camera-admin` กลับไป commit ก่อนหน้า แล้ว restart backend และ frontend
- คอลัมน์ `device_key_hash` ปล่อยไว้ได้ โค้ดเก่าไม่ได้ใช้
- ถ้าเปิด switch ไปแล้ว ให้ปิดตาม "ปิด switch" ในขั้นที่ 5 ก่อน

