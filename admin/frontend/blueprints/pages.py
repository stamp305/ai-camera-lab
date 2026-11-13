# frontend/blueprints/pages.py
"""HTML page routes for the frontend."""
from __future__ import annotations

from flask import redirect, render_template, url_for

from blueprints import bp


@bp.route("/")
def index():
    """Redirect to landing page."""
    return redirect(url_for("ai_camera.landing_page"))


@bp.route("/landing")
def landing_page():
    """Render the landing page."""
    return render_template("landing.html")


@bp.route("/users")
def users_page():
    """Render the users management page."""
    return render_template("users.html")


@bp.route("/add-user")
def add_user_page():
    """Render the add user page."""
    return render_template("add_user.html")


@bp.route("/manage_service")
def manage_service_page():
    """Render the service management page."""
    return render_template("manage_service.html")


@bp.route("/manage_package")
def manage_package_page():
    """Render the package management page."""
    return render_template("manage_package.html")


@bp.route("/version-manager")
def version_manager_page():
    """Render the Git version management page."""
    return render_template("version_manager.html")


@bp.route("/dashboard")
def dashboard_page():
    """Render the dashboard page."""
    return render_template("dashboard.html")


@bp.route("/manage_dlc/<activation_code>")
def manage_dlc_page(activation_code: str):
    """Render the DLC management page for a specific activation code."""
    return render_template("manage_dlc.html", activation_code=activation_code)
