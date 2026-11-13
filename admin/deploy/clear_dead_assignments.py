#!/usr/bin/env python3
"""ล้าง assigned_version ของสาขาที่ชี้ไปยังเวอร์ชันที่ไม่มีในทะเบียนแล้ว

assigned_version เป็น string อิสระในตาราง branches ไม่ผูกกับทะเบียนเวอร์ชัน
ลบเวอร์ชันทิ้งแล้วสาขาที่ชี้มาหามันจึงค้างเป็นตัวชี้ตาย หน้าเว็บยังโชว์ปกติ
จนกว่าจะกดอัปเดตแล้วเจอ "has no build artifact yet"

ดูอย่างเดียว: python3 deploy/clear_dead_assignments.py
ล้างจริง:     python3 deploy/clear_dead_assignments.py --apply
"""
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
REGISTRY = BACKEND / "client_code" / "versions" / "versions.json"
DB = BACKEND / "branches.db"


def main() -> int:
    apply_changes = "--apply" in sys.argv
    if not DB.exists():
        print(f"ไม่พบฐานข้อมูลสาขา: {DB}")
        return 1
    known = set(json.loads(REGISTRY.read_text())) if REGISTRY.exists() else set()
    print(f"เวอร์ชันในทะเบียน ({len(known)}): {', '.join(sorted(known)) or '(ไม่มี)'}\n")

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT activation_code, assigned_version, last_version FROM branches"
    ).fetchall()

    dead = [r for r in rows if r[1] and r[1] not in known]
    for code, assigned, last in rows:
        mark = "  <- ตัวชี้ตาย" if (assigned and assigned not in known) else ""
        print(f"  {code:<22} assigned={str(assigned):<16} last={str(last):<16}{mark}")

    if not dead:
        print("\nไม่มี assignment ที่ตาย")
        return 0
    print(f"\nเจอ {len(dead)} รายการที่ต้องล้าง")
    if not apply_changes:
        print("(ดูอย่างเดียว — ใส่ --apply เพื่อล้างจริง)")
        return 0

    # ไม่แตะ last_version: มันคือสิ่งที่สาขารายงานว่ากำลังรันอยู่จริง ล้างทิ้ง
    # แล้วจะเสียหลักฐานว่าเครื่องนั้นค้างอยู่ที่เวอร์ชันไหน
    con.executemany(
        "UPDATE branches SET assigned_version = NULL WHERE activation_code = ?",
        [(r[0],) for r in dead],
    )
    con.commit()
    print(f"ล้างแล้ว {len(dead)} รายการ (assigned_version -> NULL, last_version คงเดิม)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
