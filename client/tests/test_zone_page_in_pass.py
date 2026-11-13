"""The in-pass zone page (Traffic Detection) and its threshold panel reach in_pass_service
through the frontend route.

The page's real scripts (static/js/app.js, static/js/zone_editor.js and the inline
script of templates/draw.html) run in node against a stub DOM and a fake fetch;
checked: the URLs the page asks for and what it tells the technician.
"""
import pytest

from _zone_page import page_runner

LOCAL = {"protocol": "http:", "host": "127.0.0.1:3000", "hostname": "127.0.0.1", "pathname": "/draw",
         "href": "http://127.0.0.1:3000/draw", "search": ""}
REMOTE = {"protocol": "https:", "host": "admin.example", "hostname": "admin.example",
          "pathname": "/ai-camera/remote/abc123/draw", "href": "https://admin.example/ai-camera/remote/abc123/draw",
          "search": ""}
CAMERAS = ["/cameras/channels", 200, {}, [{"ip": "10.0.0.1", "channel": 1, "network_name": "MONO"}]]
SVC = "/api/svc/in_pass_service/"
BASE = "https://admin.example/ai-camera/remote/abc123/api/svc/in_pass_service"
SAVED = {"id": "z1", "ip": "10.0.0.1", "channel": 1, "type": "in", "color": "#f57c00", "direction": "up",
         "points": [{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.1}, {"x": 0.5, "y": 0.5}]}


@pytest.fixture
def page(tmp_path):
    return page_runner("draw.html", tmp_path)


def test_not_on_this_machine_when_the_route_says_the_service_is_missing(page):
    seen = page(LOCAL, [[SVC, 404, {"X-Svc-Missing": "1"}, {"detail": "Not Found"}], CAMERAS])
    assert seen["blockedShown"] and "not on this machine" in seen["blocked"]
    assert seen["fetched"] == ["http://127.0.0.1:3000/api/svc/in_pass_service/polygons"]


@pytest.mark.parametrize("status", [502, 503, 500, 404])  # 404 here = the service's own, without X-Svc-Missing
def test_any_other_failure_is_not_answering_yet(page, status):
    seen = page(LOCAL, [[SVC, status, {}, {"detail": "x"}], CAMERAS])
    assert seen["blockedShown"] and "not answering yet" in seen["blocked"]
    assert "not on this machine" not in seen["blocked"] and not seen["mainShown"]


def test_zone_settings_and_thresholds_go_through_the_route_and_has_dlc_is_not_asked(page):
    seen = page(REMOTE, [[SVC, 200, {}, []], CAMERAS])
    assert seen["mainShown"] and not seen["blockedShown"]
    assert seen["fetched"] == [f"{BASE}/polygons",
                               "https://admin.example/ai-camera/remote/abc123/api/proxy/cameras/channels?status=1",
                               f"{BASE}/polygons",
                               f"{BASE}/thresholds/10.0.0.1/1"]
    assert not any("has-dlc" in url or "/in-pass/" in url for url in seen["fetched"])


def test_save_puts_thresholds_and_posts_the_polygon_through_the_route(page):
    seen = page(REMOTE, [[SVC, 200, {}, []], CAMERAS], action="save",
                inputs=[["in-speed", "25"]], pick=["[data-dir]"])
    assert f"PUT {BASE}/thresholds/10.0.0.1/1" in seen["calls"]
    assert f"POST {BASE}/polygons" in seen["calls"]
    assert not [c for c in seen["calls"] if not c.startswith("GET") and SVC not in c]


def test_delete_goes_through_the_route(page):
    seen = page(REMOTE, [[SVC, 200, {}, [SAVED]], CAMERAS], action="delete")
    assert f"DELETE {BASE}/polygons/z1" in seen["calls"]
    assert not [c for c in seen["calls"] if not c.startswith("GET") and SVC not in c]
