#!/bin/bash
# ============================================================================
# Deploy ai-camera-admin ขึ้น Linux server (app.advice.th)
# รันบน SERVER เท่านั้น — ทำทีละบล็อก อ่านเหตุผลก่อนรัน
#
# สคริปต์นี้ "ช่วยพิมพ์คำสั่งให้ถูก" ไม่ได้ทำอัตโนมัติทั้งหมด เพราะบางขั้นต้อง
# ตัดสินใจ (nginx, systemd, credential ของ MySQL)
# ============================================================================
set -euo pipefail

APP_DIR="/opt/ai-camera/ai-camera-admin"   # แก้ถ้า path จริงไม่ตรงนี้
ENV_FILE="/opt/ai-camera/env.server"

# ----------------------------------------------------------------------------
# 1. ดึงโค้ด + ติดตั้ง Python packages
#
# backend กับ frontend ใช้ venv แยกกัน เพราะ dependency คนละชุด (fastapi vs
# flask) ลงรวมกันจะชนกันเอง
# ----------------------------------------------------------------------------
cd "$APP_DIR"
git pull origin main

cd "$APP_DIR/backend"
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

cd "$APP_DIR/frontend"
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# ----------------------------------------------------------------------------
# 2. .builder_key — ไม่ต้องก๊อปมาจากไหน
#
# ไฟล์นี้ commit อยู่ใน repo แล้ว มากับ git pull ข้อ 1 เรียบร้อย
# config.py อ่านจาก backend/.builder_key (path เทียบกับตัว config.py เอง)
# ย้ายที่ไม่ได้ — อ่านไม่เจอแล้วมันจะเงียบ ๆ ใช้ค่า "local-builder-key" แทน
# ทำให้ builder โดน 401 ทุกครั้งโดยไม่มีอะไรบอก
# ----------------------------------------------------------------------------
chmod 600 "$APP_DIR/backend/.builder_key"

# ----------------------------------------------------------------------------
# 3. env.server — ก๊อปแล้ว "ต้องแก้ 3 บรรทัด"
#
# DB_USER/DB_PASSWORD/DB_NAME เป็น placeholder <ตั้งจริง> อยู่ ไม่แก้แล้ว
# mysql.connector จะ fallback ไปใช้ชื่อ OS user แล้วโดน Access denied ทันที
# (เจอเองตอนเทส) — เอาค่าเดิมที่แอดมินตัวเก่าบนเครื่องนี้ใช้อยู่มาใส่
# ----------------------------------------------------------------------------
mkdir -p /opt/ai-camera
cp "$APP_DIR/deploy/env.server" "$ENV_FILE"
nano "$ENV_FILE"

# หาค่าเดิมได้จากแอดมินตัวเก่า:
#   ps aux | grep -i app_ai_camera        # หาว่า process เก่ารันจากโฟลเดอร์ไหน
#   cat <โฟลเดอร์นั้น>/.env | grep -i db
# แล้วเช็คว่า login ได้จริงก่อนไปต่อ:
#   mysql -u <user> -p <db> -e "SHOW TABLES;"
# ต้องเห็นตาราง users และมี row เท่าจำนวนสาขาที่ลงทะเบียนไว้

# ----------------------------------------------------------------------------
# 4. nginx — เปิดทาง 2 location ให้ Mac Mini builder ต่อเข้ามาได้
#
# backend ฟังแค่ 127.0.0.1:8011 ไม่ได้ผูก public IP — ต้องผ่าน nginx (โดเมน
# app.advice.th ผูกอยู่แล้ว) ไม่ต้องเปิดพอร์ต 8011 ในไฟร์วอลล์
#
# proxy_read_timeout 7d สำคัญ: Socket.IO ของ builder เป็นการเชื่อมต่อค้างยาว
# default 60s จะตัดทุกนาทีแล้ว builder จะ reconnect วนไม่จบ
# ----------------------------------------------------------------------------
sudo nano /etc/nginx/sites-available/app.advice.th
# ใส่ 2 block นี้ใน server {} ของ app.advice.th:
#
#   location /build-farm/ {
#       proxy_pass http://127.0.0.1:8011;
#       proxy_set_header Host $host;
#       proxy_set_header X-Real-IP $remote_addr;
#   }
#   location /socket.io/ {
#       proxy_pass http://127.0.0.1:8011;
#       proxy_http_version 1.1;
#       proxy_set_header Upgrade $http_upgrade;
#       proxy_set_header Connection "upgrade";
#       proxy_read_timeout 7d;
#   }
#
# ⚠️ อย่าแตะ /ai-camera/socket.io ที่มีอยู่เดิม — นั่นคือเส้นที่ 40 สาขาใช้
sudo nginx -t                 # ต้องผ่านก่อน ห้าม reload ถ้า fail
sudo systemctl reload nginx

# ----------------------------------------------------------------------------
# 5. systemd — ให้บริการขึ้นเองหลัง reboot และ restart เมื่อ crash
#
# รันทิ้งไว้ใน terminal เฉย ๆ จะตายทันทีที่ SSH หลุด
# ----------------------------------------------------------------------------
sudo cp "$APP_DIR/deploy/systemd/ai-camera-admin-backend.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/systemd/ai-camera-admin-frontend.service" /etc/systemd/system/
sudo nano /etc/systemd/system/ai-camera-admin-backend.service    # ไฟล์ตั้งค่า path/User/python ตาม .85 (/home/ubuntu/advice/python_server) — แก้ถ้าเครื่องใช้ $APP_DIR อื่น
sudo nano /etc/systemd/system/ai-camera-admin-frontend.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-camera-admin-backend ai-camera-admin-frontend
sudo systemctl status ai-camera-admin-backend ai-camera-admin-frontend --no-pager

# ----------------------------------------------------------------------------
# 6. ทดสอบ — พิสูจน์ว่าเส้นทางทำงานจริง ไม่ใช่แค่ process รันอยู่
# ----------------------------------------------------------------------------
curl -s https://app.advice.th/dlc-folders | head -c 300; echo
curl -s -o /dev/null -w "build-farm: %{http_code}\n" https://app.advice.th/build-farm/build-stages
# ถ้าพัง:
#   sudo journalctl -u ai-camera-admin-backend -n 50 --no-pager
#   sudo journalctl -u ai-camera-admin-frontend -n 50 --no-pager

echo "=== เสร็จฝั่ง server — เหลือชี้ Mac Mini builder มาที่นี่ ==="
echo "บนมินิ: sudo bash ~/ai-camera-setup/point_builder_to.sh https://app.advice.th"
