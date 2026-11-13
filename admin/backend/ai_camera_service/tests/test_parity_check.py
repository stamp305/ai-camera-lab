"""Parity verifier tests: the rebuild-and-compare logic, driven with canned rows
(issue 04). An identical row must pass; a mangled one must be reported.

Seams (.scratch/ai-camera-traffic-feed/spec.md, Testing Decisions):
  - `diff_columns` / `check_*`, called with plain dicts — no database at all, which is what
    the whole planner/executor split of 01–03 was for
  - `main()` end to end against the recording fake in tests/fakedb.py, which also proves
    the run writes nothing on either host

Run (from ai-camera-admin/backend/ai_camera_service/):
    ../.venv/bin/python -m unittest -v
"""
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from unittest import mock

from tests.fakedb import FakeDBTestCase

import app as appmod
import parity_check as pc

AVG_TABLE, ANALYTIC_TABLE = "shoping_traffic_avg", "shoping_traffic_analytic"
HOTZONE_TABLE, MONO_TABLE = "shoping_traffic_hotzone", "shoping_traffic_hotzone_mono"
DEMO_TABLE, TIER_TABLE = "emp_demo", "demo_tier"
POS_HOST = "10.10.10.70"

# The five aliases both POS queries read, as in tests/test_traffic_in_pass.py.
POS_ANSWER = {"total_sale_bill": 7, "total_bill": 9, "total_pos_bill": 6,
              "total_web_bill": 2, "total_ordecenter_bill": 1}

# The header columns every stored row carries, exactly as globalapi wrote them.
HEADER_ROW = {"branch_cuscode": "10000001", "region_id": "N", "region_name": "ภาคเหนือ",
              "province_id": 50, "province_name": "เชียงใหม่",
              "timestamps": datetime(2026, 9, 1, 9, 0, 0),      # a DATETIME column
              "insert_date_time": datetime(2026, 9, 1, 10, 2, 3),
              "zone_type": "MONO"}


def avg_row(**overrides):
    """One shoping_traffic_avg row as the PHP left it."""
    return {**HEADER_ROW, "id": 501, "hour": "09", "avg_emp": 2.5, "avg_cus": 5.0,
            "rate": 0.5, **overrides}


def analytic_row(**overrides):
    """One shoping_traffic_analytic row, its five bill columns as globalapi counted them."""
    return {**HEADER_ROW, "id": 601, "day_of_week": "Tuesday", "hour": 9, "walk_pass": 220,
            "walk_in": 40, "rate": 18.18, "count_bill": 9, "count_sale": 7, "count_pos": 6,
            "count_web": 2, "count_oc": 1, **overrides}


def hotzone_row(**overrides):
    return {**HEADER_ROW, "id": 701, "mono_id": 801, "day_of_week": "Tuesday", "hour": "09",
            "count_in": 30, "count_apple": 12, "count_nb": 8, "nb_to_apple": 3,
            "apple_to_nb": 2, "count_active": 5, "count_not_active": 4, "count_learnhub": 1,
            "engagement_wait": 1.5, **overrides}


def mono_row(**overrides):
    """The _mono child: five product zones, at least one of them counted."""
    row = {"mono_id": 801}
    for i in range(1, 6):
        row[f"name_zone{i}"], row[f"count_zone{i}"] = f"zone{i}", i
    return {**row, **overrides}


def demo_row(**overrides):
    """The emp_demo child."""
    row = {"demo_id": 901, "hotzone_id": 701}
    for i in range(1, 6):
        row[f"name_zone{i}_mono"], row[f"count_zone{i}_mono"] = f"zone{i}", i
    row["name_zone_ce"], row["count_zone_ce"] = "ce", 1
    return {**row, **overrides}


def tier_row(**overrides):
    return {"id": 1001, "hotzone_id": 701, "demo_id": 901,
            "count_zone4_tier1": 2, "count_zone4_tier2": 1, "count_zone4_tier3": 1,
            **overrides}


def children(mono=..., demo=..., tier=...):
    """The three children of a round; pass None for one the PHP never created."""
    return {MONO_TABLE: mono_row() if mono is ... else mono,
            DEMO_TABLE: demo_row() if demo is ... else demo,
            TIER_TABLE: tier_row() if tier is ... else tier}


def columns_in(check):
    """The column names a check reported, bill columns aside."""
    return {d.column for d in check.diffs}


class TestSame(unittest.TestCase):
    """The one comparison every diff rests on."""

    def test_a_float_column_read_back_as_decimal_still_matches(self):
        self.assertTrue(pc.same(0.5, Decimal("0.50")))
        self.assertTrue(pc.same(18.18, Decimal("18.18")))

    def test_a_datetime_column_matches_the_string_we_plan(self):
        self.assertTrue(pc.same("2026-09-01 09:00:00", datetime(2026, 9, 1, 9, 0, 0)))

    def test_a_number_planned_for_a_text_column_is_a_mismatch(self):
        # Issue 02 typed in-pass `hour` as int and left this question to this run: an int 9
        # reaching a varchar column holding '09' loses the padding every reader keys on.
        self.assertFalse(pc.same(9, "09"))

    def test_text_planned_for_a_numeric_column_is_no_mismatch(self):
        # The other direction is not a difference at all: a numeric column coerces '09' to
        # 9 whoever writes it, so the PHP landed the same value we would.
        self.assertTrue(pc.same("09", 9))
        self.assertTrue(pc.same("10000001", 10000001))   # branch_cuscode as an INT column

    def test_a_value_that_is_no_number_never_matches_a_numeric_column(self):
        self.assertFalse(pc.same("Tuesday", 3))
        self.assertFalse(pc.same(None, 0))

    def test_the_same_text_matches(self):
        self.assertTrue(pc.same("09", "09"))
        self.assertTrue(pc.same("MONO", "MONO"))

    def test_null_only_matches_null(self):
        self.assertTrue(pc.same(None, None))
        self.assertFalse(pc.same(0, None))
        self.assertFalse(pc.same("", None))

    def test_a_missing_column_matches_nothing_but_another_missing_one(self):
        self.assertTrue(pc.same(pc.MISSING, pc.MISSING))
        self.assertFalse(pc.same(pc.MISSING, 0))
        self.assertFalse(pc.same(5, pc.MISSING))

    def test_different_numbers_are_different(self):
        self.assertFalse(pc.same(6, 7))


class TestDiffColumns(unittest.TestCase):
    def test_a_row_we_reproduce_exactly_reports_nothing(self):
        self.assertEqual(pc.diff_columns({"walk_in": 40}, {"walk_in": 40, "id": 1}), {})

    def test_a_wrong_value_is_reported_with_both_sides(self):
        self.assertEqual(pc.diff_columns({"count_pos": 6}, {"count_pos": 5}),
                         {"count_pos": (6, 5)})

    def test_a_column_the_php_filled_and_we_never_write_is_reported(self):
        # The direction that catches a column we forgot: an update leaves it stale, an
        # insert leaves it at the column default.
        diffs = pc.diff_columns({"walk_in": 40}, {"walk_in": 40, "day_of_week": "Tuesday"})
        self.assertEqual(diffs, {"day_of_week": (pc.MISSING, "Tuesday")})

    def test_a_null_column_we_never_write_is_not_reported(self):
        # Leaving a nullable column out of an INSERT lands NULL, which is what is there.
        stored = {"walk_in": 40, "spare": None}
        self.assertEqual(pc.diff_columns({"walk_in": 40}, stored), {})

    def test_a_zero_or_blank_column_we_never_write_is_reported(self):
        # The PHP put it there and our INSERT would not — the column would come out at its
        # default instead, which is the difference this direction exists to catch.
        self.assertEqual(pc.diff_columns({}, {"unused": 0, "blank": ""}),
                         {"unused": (pc.MISSING, 0), "blank": (pc.MISSING, "")})

    def test_a_column_we_write_that_the_row_has_not_got_is_reported(self):
        self.assertEqual(pc.diff_columns({"count_oc": 1}, {}), {"count_oc": (1, pc.MISSING)})

    def test_ids_and_the_clock_are_out_of_scope(self):
        planned = {"id": 1, "mono_id": 2, "hotzone_id": 3, "demo_id": 4,
                   "insert_date_time": "2000-01-01 00:00:00"}
        stored = {"id": 99, "mono_id": 98, "hotzone_id": 97, "demo_id": 96,
                  "insert_date_time": datetime(2026, 9, 1, 10, 2, 3)}
        self.assertEqual(pc.diff_columns(planned, stored), {})


class TestBodyFields(unittest.TestCase):
    """The body is rebuilt the way the branch client builds it — out of text it holds."""

    def test_a_text_field_takes_a_numeric_column_as_text(self):
        # shoping_traffic_avg.hour as an INT column would otherwise fail every row of the
        # feed on a pydantic error, over a question `same` answers properly.
        fields = pc.body_fields(appmod.StaffingRatioRequest, avg_row(hour=9))
        self.assertEqual(fields["hour"], "9")
        self.assertEqual(appmod.StaffingRatioRequest(**fields).hour, "9")

    def test_a_text_field_keeps_the_padding_a_text_column_holds(self):
        self.assertEqual(pc.body_fields(appmod.StaffingRatioRequest, avg_row())["hour"], "09")

    def test_a_numeric_field_is_left_for_the_model_to_coerce(self):
        # in-pass types `hour` as int, so a '09' column reaches the planner as 9 — and the
        # comparison then reports the padding our INSERT would lose.
        fields = pc.body_fields(appmod.TrafficRowRequest, analytic_row(hour="09"))
        self.assertEqual(fields["hour"], "09")
        self.assertEqual(appmod.TrafficRowRequest(**fields).hour, 9)

    def test_a_null_text_column_is_left_to_be_refused(self):
        # region_id NULL is issue 03's known header gap, not something to paper over as
        # the string "None".
        with self.assertRaises(Exception):
            appmod.StaffingRatioRequest(**pc.body_fields(appmod.StaffingRatioRequest,
                                                         avg_row(region_id=None)))

    def test_the_two_renamed_header_fields_come_from_their_columns(self):
        fields = pc.body_fields(appmod.StaffingRatioRequest, avg_row())
        self.assertEqual(fields["branch"], "10000001")          # ← branch_cuscode
        self.assertEqual(fields["timestamp"], "2026-09-01 09:00:00")   # ← timestamps


class TestUpdateTargets(unittest.TestCase):
    """An update writing the right values onto the wrong row is the bug this whole feature
    exists to fix, so the row each UPDATE aims at is compared too."""

    def test_an_update_aimed_at_the_stored_row_passes(self):
        writes = [appmod.Write(MONO_TABLE, {}, ("mono_id", 801))]
        self.assertEqual(pc.update_targets(writes)[MONO_TABLE](mono_row()), {})

    def test_an_update_aimed_by_a_key_the_table_has_not_got_is_reported(self):
        # Exactly the PHP's latent bug: it filters the _mono update on an `id` column the
        # table does not carry, so the statement matches nothing and the round stays half
        # written. A regression to that WHERE must not pass this run.
        writes = [appmod.Write(MONO_TABLE, {}, ("id", 801))]
        aim = pc.update_targets(writes)[MONO_TABLE](mono_row())
        self.assertEqual(aim, {"WHERE id": (801, pc.MISSING)})

    def test_an_update_aimed_at_another_row_is_reported(self):
        writes = [appmod.Write(DEMO_TABLE, {}, ("hotzone_id", 999))]
        aim = pc.update_targets(writes)[DEMO_TABLE](demo_row())
        self.assertEqual(aim, {"WHERE hotzone_id": (999, 701)})

    def test_an_insert_has_no_target_and_a_new_id_cannot_be_one(self):
        writes = [appmod.Write(MONO_TABLE, {}, None, appmod.MONO_ID),
                  appmod.Write(HOTZONE_TABLE, {}, ("id", appmod.HOTZONE_ID))]
        self.assertEqual(pc.update_targets(writes), {})

    def test_the_update_plan_of_a_stored_round_aims_at_every_stored_child(self):
        check = pc.check_hotzone(hotzone_row(), children())
        self.assertEqual(check.diffs, [])


class TestStaffingRatioRow(unittest.TestCase):
    def check(self, row):
        req = appmod.StaffingRatioRequest(**pc.body_fields(appmod.StaffingRatioRequest, row))
        return pc.check_flat(req, pc.plan_with_clock(appmod.plan_staffing_ratio), row,
                             AVG_TABLE)

    def test_a_row_we_reproduce_exactly_passes(self):
        check = self.check(avg_row())
        self.assertEqual((check.diffs, check.bill_diffs, check.note), ([], [], None))

    def test_the_hour_keeps_its_padding_through_the_rebuild(self):
        self.assertEqual(self.check(avg_row(hour="09")).diffs, [])

    def test_a_column_the_php_filled_that_this_feed_never_writes_is_reported(self):
        # The PHP stores no day_of_week for the rate feed. One appearing in the row means
        # our planner is a column short of it.
        check = self.check(avg_row(day_of_week="Tuesday"))
        self.assertEqual(columns_in(check), {"day_of_week"})

    def test_both_plans_agree_so_the_column_is_reported_once_for_both(self):
        check = self.check(avg_row(day_of_week="Tuesday"))
        self.assertEqual([d.paths for d in check.diffs], ["insert+update"])

    def test_a_row_whose_header_will_not_rebuild_raises(self):
        # province_id NULL: the PHP stored such a round, our header refuses it. Issue 03
        # flagged it for 05; here it must be loud, not silently green.
        with self.assertRaises(Exception):
            self.check(avg_row(province_id=None))


class TestTrafficRowBills(unittest.TestCase):
    """The five columns the service counts itself — the only ones this run can really
    disagree about, and the only ones that do not fail it."""

    def check(self, row, counts):
        req = appmod.TrafficRowRequest(**pc.body_fields(appmod.TrafficRowRequest, row))
        return pc.check_flat(req, pc.plan_with_clock(appmod.plan_traffic_row, counts), row,
                             ANALYTIC_TABLE)

    def counts(self, **overrides):
        return {"count_bill": 9, "count_sale": 7, "count_pos": 6, "count_web": 2,
                "count_oc": 1, **overrides}

    def test_bills_we_count_the_same_way_pass(self):
        check = self.check(analytic_row(), self.counts())
        self.assertEqual((check.diffs, check.bill_diffs), ([], []))

    def test_a_bill_column_we_count_higher_is_reported_apart_and_does_not_fail_the_run(self):
        # More bills on our side is the expected shape: bills entered after globalapi's
        # last send for the hour are real bills it never saw.
        check = self.check(analytic_row(count_pos=5), self.counts(count_pos=6))
        self.assertEqual(check.diffs, [])                       # nothing blocking
        # A diff carries its two sides already printable, so 'ours 6' reads as it prints.
        self.assertEqual([(d.column, d.planned, d.stored) for d in check.bill_diffs],
                         [("count_pos", "6", "5")])

    def test_every_bill_column_lands_under_the_bill_heading(self):
        check = self.check(analytic_row(count_bill=0, count_sale=0, count_pos=0,
                                        count_web=0, count_oc=0), self.counts())
        self.assertEqual(check.diffs, [])
        self.assertEqual({d.column for d in check.bill_diffs}, set(pc.BILL_COLUMNS))

    def test_a_non_bill_column_still_fails_the_run(self):
        check = self.check(analytic_row(some_column_we_forgot="x"), self.counts())
        self.assertEqual(columns_in(check), {"some_column_we_forgot"})

    def test_an_hour_column_holding_zero_padded_text_is_reported(self):
        # `hour` is typed int on this feed, so a varchar column would lose the padding.
        check = self.check(analytic_row(hour="09"), self.counts())
        self.assertEqual(columns_in(check), {"hour"})


class TestHotzoneRow(unittest.TestCase):
    """Four tables: the round's own columns, each child's columns, and which children
    exist at all."""

    def test_a_round_with_all_three_children_passes(self):
        check = pc.check_hotzone(hotzone_row(), children())
        self.assertEqual((check.diffs, check.bill_diffs, check.note), ([], [], None))

    def test_a_round_the_php_left_without_an_emp_demo_row_is_reported(self):
        # Our plan writes emp_demo on every round; a stored round without one is a round
        # we would not reproduce.
        check = pc.check_hotzone(hotzone_row(), children(demo=None, tier=None))
        self.assertEqual(columns_in(check), {"(whole row)"})
        self.assertEqual({d.table for d in check.diffs}, {DEMO_TABLE})

    def test_a_mono_row_the_php_created_for_zero_zone_counts_is_reported(self):
        # Every zone count 0 → our plan writes no _mono row, so a stored one is a
        # difference in what the two write, not in a value.
        zeros = mono_row(**{f"count_zone{i}": 0 for i in range(1, 6)})
        check = pc.check_hotzone(hotzone_row(), children(mono=zeros))
        self.assertEqual({(d.table, d.column) for d in check.diffs}, {(MONO_TABLE, "(whole row)")})

    def test_a_round_with_no_zone_counts_and_no_mono_row_passes(self):
        # No _mono row means the client sent zeros for every zone, which is exactly the
        # body that makes our plan skip the table too.
        check = pc.check_hotzone(hotzone_row(mono_id=0), children(mono=None))
        self.assertEqual(check.diffs, [])

    def test_no_mac_demos_means_no_demo_tier_row_either_side(self):
        no_mac = demo_row(count_zone4_mono=0)
        check = pc.check_hotzone(hotzone_row(), children(demo=no_mac, tier=None))
        self.assertEqual(check.diffs, [])

    def test_mac_demos_with_no_demo_tier_row_is_reported(self):
        check = pc.check_hotzone(hotzone_row(), children(tier=None))
        self.assertEqual({(d.table, d.column) for d in check.diffs}, {(TIER_TABLE, "(whole row)")})

    def test_a_child_column_the_php_filled_that_we_never_write_is_reported(self):
        check = pc.check_hotzone(hotzone_row(), children(mono=mono_row(name_zone6="zone6")))
        self.assertEqual({(d.table, d.column) for d in check.diffs}, {(MONO_TABLE, "name_zone6")})

    def test_an_absent_child_folds_back_into_the_body_as_zeros(self):
        fields = pc.hotzone_body_fields(hotzone_row(), children(tier=None))
        self.assertEqual([fields[n] for n in appmod.TIER_FIELDS], [0, 0, 0])

    def test_a_stored_child_folds_back_into_the_body_verbatim(self):
        fields = pc.hotzone_body_fields(hotzone_row(), children())
        self.assertEqual(fields["count_zone3"], 3)          # from the _mono row
        self.assertEqual(fields["count_zone3_mono"], 3)     # from emp_demo
        self.assertEqual(fields["count_zone4_tier1"], 2)    # from demo_tier


class TestReadOnly(unittest.TestCase):
    """The verifier runs against production on two hosts, so this is a property of the
    process and not a promise in a docstring."""

    def setUp(self):
        self.ran = []
        self.guarded = pc.read_only(
            lambda query, params=(), **kwargs: self.ran.append((query, kwargs)))

    def test_a_select_runs(self):
        self.guarded("SELECT * FROM shoping_traffic_avg", (), db="api_data")
        self.assertEqual(len(self.ran), 1)

    def test_an_insert_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.guarded("INSERT INTO shoping_traffic_avg (id) VALUES (1)")
        self.assertEqual(self.ran, [])

    def test_an_update_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.guarded("UPDATE emp_demo SET count_zone1_mono = 1 WHERE demo_id = 1")
        self.assertEqual(self.ran, [])

    def test_a_commit_is_refused_whatever_the_statement_says(self):
        with self.assertRaises(RuntimeError):
            self.guarded("SELECT 1", (), commit=True)
        self.assertEqual(self.ran, [])

    def test_the_refusal_names_the_host_it_was_aimed_at(self):
        with self.assertRaises(RuntimeError) as caught:
            self.guarded("DELETE FROM `10000001`.ar_invoice", (), db="pos")
        self.assertIn("pos", str(caught.exception))


class TestWholeRun(FakeDBTestCase):
    """main() end to end on the fake: the day is sampled, all three feeds are reported,
    the exit status is the deploy gate — and not one statement is a write."""

    def setUp(self):
        super().setUp()
        original = appmod.run_query
        self.addCleanup(setattr, appmod, "run_query", original)
        self.env = mock.patch.dict("os.environ", {
            "POS_DB_HOST": POS_HOST, "POS_DB_USER": "pos_reader",
            "POS_DB_PASS": "pos_secret"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db.rows.update({
            AVG_TABLE: [avg_row()],
            ANALYTIC_TABLE: [analytic_row()],
            HOTZONE_TABLE: [hotzone_row()],
            MONO_TABLE: [mono_row()], DEMO_TABLE: [demo_row()], TIER_TABLE: [tier_row()],
            "10000001.ar_invoice": [dict(POS_ANSWER)],
        })

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            status = pc.main(["--date", "2026-09-01", *argv])
        return status, out.getvalue()

    def test_a_matching_day_is_all_green_and_exits_zero(self):
        status, out = self.run_main()
        self.assertIn("ALL GREEN", out)
        self.assertEqual(status, 0)

    def test_every_feed_is_reported(self):
        _, out = self.run_main()
        for table in (AVG_TABLE, ANALYTIC_TABLE, HOTZONE_TABLE):
            self.assertIn(table, out)
        self.assertEqual(out.count("rows checked    1"), 3)
        for route in ("/traffic/rate", "/traffic/in-pass", "/traffic/hotzone"):
            self.assertIn(route, out)

    def test_nothing_is_written_on_either_host(self):
        self.run_main()
        self.assert_zero_writes()
        self.assertTrue(all(sql.upper().startswith("SELECT") for sql, _ in self.db.calls))

    def test_a_mismatch_outside_the_bill_columns_exits_non_zero(self):
        self.db.rows[AVG_TABLE] = [avg_row(day_of_week="Tuesday")]
        status, out = self.run_main()
        self.assertIn("day_of_week", out)
        self.assertIn("RED", out)
        self.assertEqual(status, 1)

    def test_a_bill_mismatch_alone_stays_green(self):
        self.db.rows[ANALYTIC_TABLE] = [analytic_row(count_pos=5)]
        status, out = self.run_main()
        self.assertIn("count_pos", out)
        self.assertIn("bill columns", out)
        self.assertEqual(status, 0)

    def test_a_day_with_no_rows_is_not_a_pass(self):
        # An empty sample verifies nothing, and nothing verified must not gate a deploy.
        self.db.rows = {}
        status, out = self.run_main()
        self.assertIn("nothing to check", out)
        self.assertEqual(status, 1)

    def test_a_pos_host_that_will_not_answer_is_reported_not_skipped(self):
        self.db.fail_on = "ar_invoice"
        status, out = self.run_main()
        self.assertIn("could not check this row", out)
        self.assertEqual(status, 1)

    def test_the_sample_is_the_day_asked_for(self):
        self.run_main()
        avg_select = next(c for c in self.db.calls if AVG_TABLE in c[0])
        self.assertEqual(avg_select[1], ("2026-09-01 00:00:00", "2026-09-02 00:00:00"))

    def test_a_branch_narrows_the_sample(self):
        self.run_main("--branch", "10000001")
        avg_select = next(c for c in self.db.calls if AVG_TABLE in c[0])
        self.assertIn("branch_cuscode = %s", avg_select[0])
        self.assertEqual(avg_select[1][2], "10000001")

    def test_the_limit_reaches_the_statement(self):
        self.run_main("--limit", "5")
        avg_select = next(c for c in self.db.calls if AVG_TABLE in c[0])
        self.assertTrue(avg_select[0].endswith("LIMIT 5"))


class TestDefaults(unittest.TestCase):
    def test_the_default_day_is_three_days_back(self):
        args = pc.parse_args([])
        expected = datetime.now().date() - pc.timedelta(days=pc.SETTLED_DAYS)
        self.assertEqual(args.date, expected.strftime(pc.DAY_FMT))

    def test_a_day_is_sampled_across_every_branch_unless_one_is_named(self):
        self.assertIsNone(pc.parse_args([]).branch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
