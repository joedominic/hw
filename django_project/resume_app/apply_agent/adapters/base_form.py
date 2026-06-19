"""
Shared base for selector-driven ATS adapters (Greenhouse, Lever, Ashby, iCIMS).

Subclasses declare host markers, a field-selector map, resume upload selectors,
submit selectors, and confirmation signals. The base implements the dry-run
fill, the re-validation fill (fresh form, answer key as input), and the atomic
submit-and-verify (DOM confirmation AND network response in one transaction).

DOM interaction is defensive: a missing optional field is recorded, not fatal.
``build_answer_key`` is pure so it can be unit tested without a browser.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..base import ApplyContext, FillResult, SubmitResult, standard_answer_key

logger = logging.getLogger("huey")


class FormAdapter:
    ats_id: str = ""
    host_markers: tuple[str, ...] = ()
    # answer-key name -> ordered candidate CSS selectors
    field_selectors: dict[str, list[str]] = {}
    # answer-key names that must be present for a high-confidence fill
    required_fields: tuple[str, ...] = ("first_name", "last_name", "email")
    resume_input_selectors: tuple[str, ...] = ("input[type=file]",)
    submit_selectors: tuple[str, ...] = ("button[type=submit]",)
    confirmation_text_markers: tuple[str, ...] = (
        "thank you for applying",
        "application submitted",
        "your application has been submitted",
        "successfully submitted",
    )
    confirmation_url_markers: tuple[str, ...] = ("confirmation", "thank", "submitted")
    # XHR/fetch URL substrings that indicate the submit POST.
    submit_network_markers: tuple[str, ...] = ()

    # ----- classification ---------------------------------------------------
    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        host = (urlparse(url).hostname or "").lower()
        return any(marker in host for marker in self.host_markers)

    # ----- pure answer-key building -----------------------------------------
    def build_answer_key(self, ctx: ApplyContext) -> dict:
        return standard_answer_key(ctx)

    # ----- fill passes ------------------------------------------------------
    def fill_application(self, ctx: ApplyContext, *, stop_before_submit: bool) -> FillResult:
        payload = self.build_answer_key(ctx)
        return self._do_fill(ctx, payload, step="dry_run_fill")

    def fill_from_payload(self, ctx: ApplyContext, payload: dict) -> FillResult:
        # Re-validation: always load a clean form; never replay stored DOM/tokens.
        return self._do_fill(ctx, dict(payload or {}), step="revalidation_fill")

    def _do_fill(self, ctx: ApplyContext, payload: dict, *, step: str) -> FillResult:
        page = ctx.page
        if page is None:
            return FillResult(ok=False, error_code="fill_failed", message="No browser page")

        try:
            page.goto(ctx.apply_url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            return FillResult(ok=False, error_code="fill_failed", message=f"navigation failed: {e}")

        filled: list[str] = []
        missing: list[str] = []
        for key, selectors in self.field_selectors.items():
            value = payload.get(key)
            if value in (None, ""):
                continue
            if self._fill_first(page, selectors, value):
                filled.append(key)
            else:
                missing.append(key)

        uploaded = self._upload_resume(ctx)
        confidence = self._confidence(payload, filled, uploaded)
        screenshot_path = ""
        if ctx.attempt_id:
            from ..step_capture import save_step_screenshot

            try:
                png = page.screenshot(type="png")
                screenshot_path = save_step_screenshot(ctx.attempt_id, step, png)
            except Exception:
                screenshot_path = ""
        ctx.log(
            step,
            message=f"filled={filled} missing={missing} uploaded={uploaded}",
            action_snapshot={"filled": filled, "missing": missing, "uploaded": uploaded, "url": page.url},
            screenshot_path=screenshot_path,
        )
        missing_required = [f for f in self.required_fields if f in missing]
        ok = not missing_required and uploaded
        return FillResult(
            ok=ok,
            confidence=confidence,
            payload=payload,
            missing_fields=missing,
            error_code="" if ok else "fill_failed",
            message="" if ok else f"missing required: {missing_required or 'resume upload'}",
        )

    # ----- atomic submit ----------------------------------------------------
    def submit_and_verify(self, ctx: ApplyContext) -> SubmitResult:
        page = ctx.page
        if page is None:
            return SubmitResult(ok=False, error_code="fill_failed", message="No browser page")

        network: list[dict] = []
        self._attach_network_listener(page, network)

        clicked = self._click_first(page, self.submit_selectors)
        if not clicked:
            return SubmitResult(
                ok=False,
                error_code="fill_failed",
                message="Submit button not found",
                network_log=network,
            )

        try:
            page.wait_for_load_state("networkidle")
        except Exception:
            pass

        dom_ok = self._detect_confirmation_dom(page)
        net_ok = self._detect_confirmation_network(network)
        confirmed = dom_ok and (net_ok or not self.submit_network_markers)
        ctx.log(
            "submit",
            message=f"dom_ok={dom_ok} net_ok={net_ok}",
            action_snapshot={"dom_ok": dom_ok, "net_ok": net_ok},
            network_log=network,
        )
        if confirmed:
            return SubmitResult(ok=True, confirmed=True, network_log=network)
        # Click happened but confirmation is ambiguous — do not assume success.
        return SubmitResult(
            ok=False,
            confirmed=False,
            error_code="submit_ambiguous",
            message="Submit clicked but confirmation was not detected.",
            network_log=network,
        )

    # ----- DOM helpers ------------------------------------------------------
    def _fill_first(self, page, selectors, value) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() == 0:
                    continue
                tag = (loc.evaluate("el => el.tagName") or "").lower()
                if tag == "select":
                    loc.select_option(label=str(value))
                else:
                    loc.fill(str(value))
                return True
            except Exception:
                continue
        return False

    def _click_first(self, page, selectors) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() == 0:
                    continue
                loc.click()
                return True
            except Exception:
                continue
        return False

    def _upload_resume(self, ctx: ApplyContext) -> bool:
        if not ctx.resume_file_path:
            return False
        for selector in self.resume_input_selectors:
            try:
                loc = ctx.page.locator(selector).first
                if loc.count() == 0:
                    continue
                loc.set_input_files(ctx.resume_file_path)
                return True
            except Exception:
                continue
        return False

    def _attach_network_listener(self, page, sink: list) -> None:
        if not self.submit_network_markers:
            return

        def _on_response(response):
            try:
                url = response.url
                if any(marker in url for marker in self.submit_network_markers):
                    sink.append({"url": url, "status": response.status})
            except Exception:
                pass

        with _suppress():
            page.on("response", _on_response)

    def _detect_confirmation_dom(self, page) -> bool:
        try:
            text = (page.content() or "").lower()
        except Exception:
            text = ""
        if any(marker in text for marker in self.confirmation_text_markers):
            return True
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        return any(marker in url for marker in self.confirmation_url_markers)

    def _detect_confirmation_network(self, network: list) -> bool:
        return any(200 <= int(entry.get("status", 0)) < 300 for entry in network)

    def _confidence(self, payload: dict, filled: list, uploaded: bool) -> float:
        expected = [k for k in payload if k in self.field_selectors]
        if not expected:
            base = 0.5
        else:
            base = len(filled) / max(1, len(expected))
        if not uploaded:
            base *= 0.5
        return round(min(1.0, base), 3)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
