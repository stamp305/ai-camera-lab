"""Ticket 17: Live Display reads each zone service through the frontend route (B27), and the
zone editor's picture is camera_service's processed frame.

The pages' real scripts run in node (tests/_zone_page.py); checked: the URLs asked for, the
live view's src and what the page shows.
"""
import re

import pytest

from _zone_page import page_runner

LOCAL = {"protocol": "http:", "host": "127.0.0.1:3000", "hostname": "127.0.0.1", "pathname": "/display",
         "href": "http://127.0.0.1:3000/display", "search": ""}
REMOTE = {"protocol": "https:", "host": "admin.example", "hostname": "admin.example",
          "pathname": "/ai-camera/remote/abc123/display", "href": "https://admin.example/ai-camera/remote/abc123/display",
          "search": ""}
RBASE = "https://admin.example/ai-camera/remote/abc123/api/svc"
LBASE = "http://127.0.0.1:3000/api/svc"
SERVICES = ["in_pass_service", "hotzone_service", "face_service", "rate_service"]
CAMERAS = ["/cameras/channels", 200, {}, [{"ip": "10.0.0.1", "channel": 1, "network_name": "MONO"},
                                          {"ip": "10.0.0.2", "channel": 3, "network_name": "BACK"}]]
PTS = [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.5}]
ZONES = {
    "in_pass_service": [{"id": "i1", "ip": "10.0.0.1", "channel": 1, "type": "in", "color": "#f57c00",
                         "direction": "up", "points": PTS}],
    "hotzone_service": [{"id": "h1", "ip": "10.0.0.1", "channel": 1, "color": "#16a34a", "name": "Entrance",
                         "duration": 60, "points": PTS}],
    "face_service": [{"id": "f1", "ip": "10.0.0.2", "channel": 3, "color": "#2563eb", "points": PTS}],
    "rate_service": [{"id": "r1", "ip": "10.0.0.1", "channel": 1, "type": "employee", "color": "#9333ea",
                      "points": PTS}],
}
MISSING = [404, {"X-Svc-Missing": "1"}, {"detail": "Not Found"}]


def answers(**down):
    """Every service answering its zones, except those in ``down`` ({service: [status, headers, body]})."""
    return [[f"/api/svc/{s}/", *down.get(s, [200, {}, ZONES[s]])] for s in SERVICES] + [CAMERAS]


@pytest.fixture
def page(tmp_path):
    return page_runner("display.html", tmp_path)


def shown(seen, el_id):
    return seen["ids"].get(el_id, {"shown": False})["shown"]  # never touched = still hidden


def src_path(seen):
    return seen["ids"]["stream"]["src"].split("?")[0]


def test_reads_every_service_zone_settings_through_the_route_under_the_tunnel(page):
    seen = page(REMOTE, answers())
    assert sorted(seen["fetched"]) == sorted([f"{RBASE}/{s}/polygons" for s in SERVICES]
                                             + ["https://admin.example/ai-camera/remote/abc123/api/proxy/cameras/channels?status=1"])
    assert not any("/monitor" in u for u in seen["fetched"])
    # first stream = first selected camera's first service in the old feed's order: in-pass
    assert src_path(seen) == f"{RBASE}/in_pass_service/stream/cam_10_0_0_1_1"
    assert shown(seen, "main") and "Walk In" in seen["ids"]["list"]["html"]
    assert seen["ids"]["card-title"]["text"] == "MONO - Camera 1"


def test_live_view_is_the_services_own_stream_at_the_page_host(page):
    seen = page(LOCAL, answers(), action="menu", menu=["svc-btn", "svc-menu"], choose="rate")
    assert seen["menu"] == ["in-pass", "hotzone", "rate"]  # the services that have zones on this camera
    assert src_path(seen) == f"{LBASE}/rate_service/stream/cam_10_0_0_1_1"
    assert "Employee" in seen["ids"]["list"]["html"] and "Walk In" not in seen["ids"]["list"]["html"]


def test_camera_menu_lists_each_camera_once_and_reaches_face_streams(page):
    seen = page(LOCAL, answers(), action="menu", menu=["cam-btn", "cam-menu"], choose="BACK - Camera 3")
    assert seen["menu"] == ["MONO - Camera 1", "BACK - Camera 3"]
    assert src_path(seen) == f"{LBASE}/face_service/stream/cam_10_0_0_2_3"
    assert "Attendance" in seen["ids"]["list"]["html"]


def test_a_service_not_on_this_machine_is_skipped(page):
    seen = page(LOCAL, answers(in_pass_service=MISSING), action="menu", menu=["svc-btn", "svc-menu"], choose="")
    assert seen["menu"] == ["hotzone", "rate"]
    assert src_path(seen) == f"{LBASE}/hotzone_service/stream/cam_10_0_0_1_1"
    assert "Entrance" in seen["ids"]["list"]["html"] and shown(seen, "main")


@pytest.mark.parametrize("down", [[502, {}, {"detail": "Bad Gateway"}], [504, {}, {"detail": "Gateway Timeout"}],
                                  [0, {}, None],  # the request itself failed
                                  [404, {}, {"detail": "Not Found"}]])  # the service's own 404, no X-Svc-Missing
def test_one_service_stopped_leaves_the_others_on_screen(page, down):
    seen = page(LOCAL, answers(in_pass_service=down), action="menu", menu=["svc-btn", "svc-menu"], choose="rate")
    assert shown(seen, "main") and not shown(seen, "empty")
    assert seen["menu"] == ["hotzone", "rate"]
    assert src_path(seen) == f"{LBASE}/rate_service/stream/cam_10_0_0_1_1"
    assert "Employee" in seen["ids"]["list"]["html"]


def test_cameras_not_selected_are_not_listed(page):
    only_back = ["/cameras/channels", 200, {}, [{"ip": "10.0.0.2", "channel": 3, "network_name": "BACK"}]]
    seen = page(LOCAL, answers()[:-1] + [only_back], action="menu", menu=["cam-btn", "cam-menu"], choose="")
    assert seen["menu"] == ["BACK - Camera 3"]
    assert src_path(seen) == f"{LBASE}/face_service/stream/cam_10_0_0_2_3"


@pytest.mark.parametrize("down", [[502, {}, {"detail": "Bad Gateway"}],
                                  [404, {}, {"detail": "Not Found"}]])  # the service's own 404 is not "missing"
def test_no_service_answering_says_so(page, down):
    seen = page(LOCAL, answers(**{s: down for s in SERVICES}))
    assert shown(seen, "empty") and not shown(seen, "main")
    assert seen["ids"]["empty-title"]["text"] == "Could not load streams"


def test_a_hung_service_is_given_up_and_the_others_shown(page):
    # connects but never answers; the frontend route would hold it for 60 s
    seen = page(LOCAL, answers(in_pass_service=[-1, {}, None]), wait=3300,
                action="menu", menu=["svc-btn", "svc-menu"], choose="rate")
    assert shown(seen, "main") and seen["menu"] == ["hotzone", "rate"]
    assert src_path(seen) == f"{LBASE}/rate_service/stream/cam_10_0_0_1_1"


@pytest.mark.parametrize("cameras", [[0, {}, None],  # no answer
                                     [500, {}, {"detail": "db down"}],
                                     [200, {}, {"detail": "not a list"}],  # malformed body
                                     [200, {}, [None]]])
def test_camera_list_unreadable_says_so_instead_of_listing_every_zone(page, cameras):
    seen = page(LOCAL, answers()[:-1] + [["/cameras/channels", *cameras]])
    assert shown(seen, "empty") and not shown(seen, "main") and not shown(seen, "loading")
    assert seen["ids"]["empty-title"]["text"] == "Could not load streams"
    assert seen["ids"]["stream"]["src"] == ""  # no live view picked


def test_live_view_src_has_a_cache_buster(page):
    seen = page(LOCAL, answers())
    assert re.fullmatch(rf"{re.escape(LBASE)}/in_pass_service/stream/cam_10_0_0_1_1\?t=\d+", seen["ids"]["stream"]["src"])


def test_a_live_view_that_fails_to_load_shows_stream_unavailable(page):
    seen = page(LOCAL, answers(), fire=[["stream", "error"]])
    assert shown(seen, "stream-error") and shown(seen, "main")
    seen = page(LOCAL, answers(), fire=[["stream", "error"], ["stream", "load"]])
    assert not shown(seen, "stream-error")


def test_no_zone_service_on_this_machine_is_just_no_streams(page):
    seen = page(LOCAL, answers(**{s: MISSING for s in SERVICES}))
    assert shown(seen, "empty") and not shown(seen, "main")
    assert "empty-title" not in seen["ids"]  # never touched: the template's own "No live streams" stays


@pytest.mark.parametrize("template,svc", [("draw.html", "in_pass_service"), ("zone_draw.html", "hotzone_service"),
                                          ("rate.html", "rate_service"), ("attendance.html", "face_service")])
@pytest.mark.parametrize("location,base", [(LOCAL, "http://127.0.0.1:3000"),
                                           (REMOTE, "https://admin.example/ai-camera/remote/abc123")])
def test_zone_editor_picture_is_camera_service_processed_frame(tmp_path, template, svc, location, base):
    run = page_runner(template, tmp_path)
    seen = run(location, [[f"/api/svc/{svc}/", 200, {}, []],
                          ["/cameras/channels", 200, {}, [{"ip": "10.0.0.12", "channel": 2, "network_name": "MONO"}]]])
    assert seen["mainShown"]
    assert seen["img"] == f"{base}/api/svc/camera_service/frames/cam_10_0_0_12_2"
