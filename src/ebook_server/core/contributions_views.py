"""Pages for contribution recognition (#27): scorecard, collective stats, leaderboard, admin.

Every view answers 404 while ``BOOK_SCANNER_CONTRIBUTIONS_ENABLED`` is false; the
leaderboard additionally needs ``BOOK_SCANNER_LEADERBOARD_ENABLED``. Pages render HTML
for browsers (``Accept: text/html`` or ``?view=html``) and JSON otherwise, like the
rest of the app.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseBase,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import contributions
from .models import AppUserProfile, ContributionEvent, ReviewTask
from .views import _active_library_profile, _request_payload, _wants_html

ERASE_CONFIRMATION = "erase"


def contribution_flags(request: HttpRequest) -> dict[str, bool]:
    """Template context: whether to show the contribution links in the navigation."""
    is_enabled = contributions.enabled()
    admin_link = False
    if is_enabled and getattr(request, "user", None) is not None:
        profile = _active_library_profile(request)
        admin_link = profile is not None and profile.is_admin()
    return {
        "contributions_enabled": is_enabled,
        "leaderboard_enabled": contributions.leaderboard_enabled(),
        "contributions_admin_link": admin_link,
    }


def _require_enabled() -> None:
    if not contributions.enabled():
        raise Http404("contribution recognition is disabled")


def _member(request: HttpRequest) -> AppUserProfile | None:
    _require_enabled()
    return _active_library_profile(request)


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


def _event_row(event: ContributionEvent) -> dict[str, Any]:
    return {
        "id": str(event.pk),
        "kind": event.kind,
        "kind_label": event.get_kind_display(),
        "state": event.state,
        "points": _decimal_str(event.points) if event.points is not None else None,
        "own_upload": event.own_upload,
        "reason": event.reason,
        "review_task_id": event.review_task_id,
        "scan_job_id": str(event.scan_job_id) if event.scan_job_id else None,
        "occurred_at": event.occurred_at.isoformat(),
        "settle_after": event.settle_after.isoformat(),
    }


# --- Member pages ------------------------------------------------------------------------


@require_GET
@login_required
def my_contributions(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if profile is None:
        return HttpResponseForbidden()
    card = contributions.scorecard(profile.user)
    open_work = contributions.open_work_for(profile)
    if _wants_html(request):
        return render(
            request,
            "core/contributions_scorecard.html",
            {
                "card": card,
                "open_work": open_work,
                "hold_days": contributions.hold_period().days,
                "error": request.GET.get("error", ""),
            },
        )
    preference = card["preference"]
    return JsonResponse(
        {
            "all_time_points": _decimal_str(card["all_time_points"]),
            "season_points": _decimal_str(card["season_points"]),
            "season_start": card["season_start"].isoformat(),
            "pending_counts": card["pending_counts"],
            "accuracy": card["accuracy"],
            "open_reviews_you_can_resolve": open_work,
            "leaderboard_opt_in": bool(preference and preference.leaderboard_opt_in),
            "display_handle": preference.display_handle if preference else "",
            "recent": [_event_row(event) for event in card["recent"]],
        },
    )


@require_POST
@login_required
def my_leaderboard_preference(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if profile is None:
        return HttpResponseForbidden()
    payload = _request_payload(request)
    opt_in = str(payload.get("opt_in", "")).lower() in {"1", "true", "on", "yes"}
    if opt_in and str(payload.get("consent", "")).lower() not in {"1", "true", "on", "yes"}:
        return _preference_error(request, "please confirm the consent statement")
    try:
        preference = contributions.set_leaderboard_opt_in(
            user=profile.user,
            opt_in=opt_in,
            handle=str(payload.get("display_handle", "")),
        )
    except contributions.PreferenceError as exc:
        return _preference_error(request, str(exc))
    if _wants_html(request):
        return redirect("/me/contributions/?view=html")
    return JsonResponse(
        {
            "leaderboard_opt_in": preference.leaderboard_opt_in,
            "display_handle": preference.display_handle,
        },
    )


def _preference_error(request: HttpRequest, message: str) -> HttpResponseBase:
    if _wants_html(request):
        from urllib.parse import urlencode

        return redirect("/me/contributions/?" + urlencode({"view": "html", "error": message}))
    return HttpResponseBadRequest(message)


def _export_response(data: dict[str, Any], filename: str) -> HttpResponse:
    response = HttpResponse(
        json.dumps(data, indent=2, sort_keys=True),
        content_type="application/json",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


@require_GET
@login_required
def my_contributions_export(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if profile is None:
        return HttpResponseForbidden()
    return _export_response(
        contributions.export_user_data(profile.user),
        "my-contributions.json",
    )


@require_POST
@login_required
def my_contributions_erase(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if profile is None:
        return HttpResponseForbidden()
    payload = _request_payload(request)
    if str(payload.get("confirm", "")).strip().lower() != ERASE_CONFIRMATION:
        return _preference_error(request, f"type '{ERASE_CONFIRMATION}' to confirm")
    result = contributions.erase_user_data(profile.user)
    if _wants_html(request):
        return redirect("/me/contributions/?view=html")
    return JsonResponse({"erased": result})


# --- Shared pages ------------------------------------------------------------------------


@require_GET
@login_required
def collective_stats(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if profile is None:
        return HttpResponseForbidden()
    stats = contributions.collective_stats()
    if _wants_html(request):
        return render(request, "core/contributions_stats.html", {"stats": stats})
    return JsonResponse(stats)


@require_GET
@login_required
def leaderboard(request: HttpRequest) -> HttpResponseBase:
    profile = _member(request)
    if not contributions.leaderboard_enabled():
        raise Http404("the leaderboard is disabled")
    if profile is None:
        return HttpResponseForbidden()
    period = "all" if request.GET.get("period") == "all" else "season"
    rows = contributions.leaderboard(period=period)
    season_start = contributions.season_start()
    if _wants_html(request):
        return render(
            request,
            "core/contributions_leaderboard.html",
            {
                "rows": rows,
                "period": period,
                "season_start": season_start,
                "season_kind": contributions.season_kind(),
            },
        )
    return JsonResponse(
        {
            "period": period,
            "season_start": season_start.isoformat(),
            "rows": [{**row, "points": _decimal_str(row["points"])} for row in rows],
        },
    )


# --- Admin ---------------------------------------------------------------------------------


def _admin(request: HttpRequest) -> AppUserProfile | None:
    profile = _member(request)
    if profile is None or not profile.is_admin():
        return None
    return profile


def _target_user(payload: dict[str, Any]) -> Any:
    username = str(payload.get("username", "")).strip()
    if not username:
        raise contributions.ContributionAdminError("username required")
    user = get_user_model().objects.filter(username__iexact=username).first()
    if user is None:
        raise contributions.ContributionAdminError("no such user")
    return user


def _parse_decimal(raw: object, *, name: str, allow_blank: bool = False) -> Decimal | None:
    text = str(raw if raw is not None else "").strip()
    if not text and allow_blank:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise contributions.ContributionAdminError(f"{name} must be a number") from exc
    if not value.is_finite() or abs(value) > Decimal("10000"):
        raise contributions.ContributionAdminError(f"{name} is out of range")
    return value


def _apply_admin_action(action: str, payload: dict[str, Any], actor: Any) -> str:
    if action == "adjust":
        user = _target_user(payload)
        points = _parse_decimal(payload.get("points"), name="points")
        assert points is not None
        contributions.admin_adjust(
            user=user,
            points=points,
            reason=str(payload.get("reason", "")),
            actor=actor,
        )
        return f"adjusted {user.get_username()} by {points}"
    if action == "void":
        try:
            event = ContributionEvent.objects.get(pk=str(payload.get("event_id", "")).strip())
        except (ContributionEvent.DoesNotExist, ValueError) as exc:
            raise contributions.ContributionAdminError("no such event") from exc
        contributions.admin_void(event=event, reason=str(payload.get("reason", "")), actor=actor)
        return f"voided {event.pk}"
    if action in {"exclude", "include"}:
        user = _target_user(payload)
        contributions.set_leaderboard_exclusion(
            user=user,
            excluded=action == "exclude",
            reason=str(payload.get("reason", "")),
        )
        return f"{action}d {user.get_username()}"
    if action == "flag_review":
        try:
            task = ReviewTask.objects.get(pk=int(str(payload.get("review_task_id", "")).strip()))
        except (ReviewTask.DoesNotExist, ValueError) as exc:
            raise contributions.ContributionAdminError("no such review task") from exc
        contributions.admin_flag_review_wrong(
            task=task,
            note=str(payload.get("note", "")),
            actor=actor,
        )
        return f"flagged review {task.pk}"
    if action == "weights":
        specs: dict[str, contributions.WeightSpec] = {}
        for kind, _label in contributions.iter_kinds():
            base = _parse_decimal(payload.get(f"{kind}__base"), name=f"{kind} base")
            per_unit = _parse_decimal(payload.get(f"{kind}__per_unit"), name=f"{kind} per unit")
            cap = _parse_decimal(payload.get(f"{kind}__cap"), name=f"{kind} cap", allow_blank=True)
            own = _parse_decimal(payload.get(f"{kind}__own"), name=f"{kind} own-upload")
            assert base is not None and per_unit is not None and own is not None
            if base < 0 or per_unit < 0 or own < 0 or (cap is not None and cap < 0):
                raise contributions.ContributionAdminError("weights cannot be negative")
            specs[kind] = contributions.WeightSpec(base, per_unit, cap, own)
        version = contributions.save_weights(specs, actor=actor)
        return f"saved weights version {version}"
    if action == "erase":
        user = _target_user(payload)
        if str(payload.get("confirm", "")).strip().lower() != ERASE_CONFIRMATION:
            raise contributions.ContributionAdminError(f"type '{ERASE_CONFIRMATION}' to confirm")
        contributions.erase_user_data(user)
        return f"erased contribution data of {user.get_username()}"
    if action == "settle":
        report = contributions.settle()
        return (
            f"settled {report.settled}, voided {report.voided}, "
            f"clawed back {report.clawed_back}, pruned {report.pruned}"
        )
    raise contributions.ContributionAdminError("unknown action")


@require_http_methods(["GET", "POST"])
@login_required
def contributions_admin(request: HttpRequest) -> HttpResponseBase:
    profile = _admin(request)
    if profile is None:
        return HttpResponseForbidden()
    message = ""
    error = ""
    if request.method == "POST":
        payload = _request_payload(request)
        if payload.get("action") == "export":
            try:
                user = _target_user(payload)
            except contributions.ContributionAdminError as exc:
                return HttpResponseBadRequest(str(exc))
            return _export_response(
                contributions.export_user_data(user),
                f"contributions-{user.pk}.json",
            )
        try:
            message = _apply_admin_action(str(payload.get("action", "")), payload, profile.user)
        except contributions.ContributionAdminError as exc:
            error = str(exc)
        if not _wants_html(request):
            if error:
                return HttpResponseBadRequest(error)
            return JsonResponse({"ok": True, "message": message})
    version = contributions.current_weight_version()
    weights = contributions.weights_for_version(version)
    if not _wants_html(request):
        return JsonResponse(
            {
                "weight_version": version,
                "weights": {
                    kind: {
                        "base_points": _decimal_str(spec.base_points),
                        "per_unit_points": _decimal_str(spec.per_unit_points),
                        "cap": _decimal_str(spec.cap) if spec.cap is not None else None,
                        "own_upload_multiplier": _decimal_str(spec.own_upload_multiplier),
                    }
                    for kind, spec in weights.items()
                },
            },
        )
    recent = ContributionEvent.objects.select_related("user").order_by("-created_at")[:100]
    return render(
        request,
        "core/contributions_admin.html",
        {
            "message": message,
            "error": error,
            "weight_version": version,
            "weight_rows": [
                (kind, label, weights[kind]) for kind, label in contributions.iter_kinds()
            ],
            "recent": recent,
            "hold_days": contributions.hold_period().days,
        },
        status=400 if error else 200,
    )


@require_GET
@login_required
def contributions_admin_export(request: HttpRequest, username: str) -> HttpResponseBase:
    profile = _admin(request)
    if profile is None:
        return HttpResponseForbidden()
    user = get_user_model().objects.filter(username__iexact=username).first()
    if user is None:
        raise Http404("no such user")
    return _export_response(
        contributions.export_user_data(user),
        f"contributions-{user.pk}.json",
    )
