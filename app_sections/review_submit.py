"""Review/submit and submission-complete renderers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import streamlit as st

from config import COMPANY_NAME
from pdf_generator import (
    generate_application_pdf,
    generate_clearinghouse_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
)
from services.document_service import render_supporting_documents_section, sync_pending_uploads
from services.draft_service import autosave_draft
from services.notification_service import send_internal_submission_notification
from services.submission_service import build_submission_artifacts, save_submission_bundle
from state import prev_page, reset_application_state
from submission_storage import get_submission_destination_summary
from ui.common import show_missing_fields, summary_item


def _attempt_submission_notification() -> None:
    if st.session_state.get("submission_notification_sent"):
        return
    if st.session_state.get("submission_notification_status_code") == "disabled":
        return

    saved_submission_dir = st.session_state.get("saved_submission_dir")
    if not saved_submission_dir:
        return

    notification_result = send_internal_submission_notification(
        form_data=st.session_state.form_data,
        submission_result={"location_label": saved_submission_dir},
        uploaded_documents=st.session_state.get("uploaded_documents", []),
    )
    status = notification_result.get("status")
    st.session_state.submission_notification_status_code = status

    if status == "sent":
        st.session_state.submission_notification_sent = True
        st.session_state.submission_notification_status = notification_result.get("message")
        st.session_state.submission_notification_error = None
    elif status == "disabled":
        st.session_state.submission_notification_status = notification_result.get("message")
        st.session_state.submission_notification_error = None
    else:
        st.session_state.submission_notification_status = None
        st.session_state.submission_notification_error = notification_result.get("message")


def render_review_submit_page(submissions_dir: Path) -> None:
    st.subheader("🧾 Review & Submit")
    submission_destination = get_submission_destination_summary(submissions_dir)
    st.info(
        f"When you submit, a company copy will be saved to {submission_destination}. "
        "If SMTP is configured, an internal notification email will also be sent without attachments."
    )

    with st.expander("Personal Information", expanded=True):
        summary_item(
            "Applicant",
            f"{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}".strip(),
        )
        summary_item("Date of birth", st.session_state.form_data.get("dob"))
        summary_item(
            "Address",
            f"{st.session_state.form_data.get('address', '')}, {st.session_state.form_data.get('city', '')}, "
            f"{st.session_state.form_data.get('state', '')} {st.session_state.form_data.get('zip_code', '')}".strip(", "),
        )
        summary_item("Primary phone", st.session_state.form_data.get("primary_phone"))
        summary_item("Email", st.session_state.form_data.get("email"))
        summary_item(
            "Emergency contact",
            f"{st.session_state.form_data.get('emergency_name', '')} / {st.session_state.form_data.get('emergency_phone', '')}",
        )

    with st.expander("Company Questions & Experience"):
        summary_item("Position", st.session_state.form_data.get("position"))
        summary_item(
            "Preferred office for onboarding",
            st.session_state.form_data.get("preferred_office") or st.session_state.form_data.get("applying_location"),
        )
        summary_item(
            "Currently employed/contracted elsewhere",
            st.session_state.form_data.get("currently_employed"),
        )
        summary_item("Previously contracted here", st.session_state.form_data.get("worked_here_before"))
        summary_item("TWIC card", st.session_state.form_data.get("twic_card"))
        summary_item("Referral source", st.session_state.form_data.get("referral_source"))
        summary_item("License entries", len(st.session_state.licenses), default="0")
        summary_item("Employment history entries", len(st.session_state.employers), default="0")

    with st.expander("Education, Safety, and Records"):
        summary_item("Highest grade completed", st.session_state.form_data.get("highest_grade"))
        summary_item("Attended trucking school", st.session_state.form_data.get("attended_trucking_school"))
        summary_item("Accidents reported", len(st.session_state.accidents), default="0")
        summary_item("Violations reported", len(st.session_state.violations), default="0")
        summary_item("Currently disqualified", st.session_state.form_data.get("disq_391_15"))
        summary_item(
            "Suspended or revoked license history",
            st.session_state.form_data.get("disq_suspended"),
        )
        summary_item("Supporting documents uploaded", len(st.session_state.get("uploaded_documents", [])), default="0")

    with st.expander("Disclosures & Acknowledgments"):
        summary_item("Drug & alcohol policy", st.session_state.form_data.get("drug_alcohol_cert"))
        summary_item("Applicant certification", st.session_state.form_data.get("applicant_cert"))
        summary_item("FCRA acknowledged", st.session_state.form_data.get("fcra_acknowledge"))
        if st.session_state.form_data.get("ca_applicable"):
            summary_item(
                "California disclosure acknowledged",
                st.session_state.form_data.get("ca_disclosure_acknowledge"),
            )
        else:
            summary_item("California disclosure", "Not applicable")
        summary_item("Consumer copy requested", st.session_state.form_data.get("ca_copy"))
        summary_item("PSP acknowledged", st.session_state.form_data.get("psp_acknowledge"))
        summary_item("Clearinghouse acknowledged", st.session_state.form_data.get("clearinghouse_acknowledge"))
        summary_item(
            "Investigative consumer report acknowledged",
            st.session_state.form_data.get("inv_consumer_report"),
        )

    render_supporting_documents_section()

    st.markdown("---")
    st.markdown("### What happens when you submit")
    st.markdown(
        "1. A timestamped submission bundle is created for this applicant.\n"
        f"2. The app saves `submission.json` plus PDF copies of the application and disclosures to {submission_destination}.\n"
        "3. Any uploaded PDF/JPG/PNG supporting documents are stored in the secure backend and linked to the submission record.\n"
        "4. The applicant can manually download copies from the confirmation page.\n"
        "5. If SMTP is configured, an internal notification email is sent without attachments or SSN data."
    )

    review_confirm = st.checkbox(
        "I reviewed the information above and I am ready to submit this application.",
        value=st.session_state.form_data.get("review_confirm", False),
    )

    bcol1, bcol2, bcol3 = st.columns(3)
    with bcol1:
        if st.button("← Back", key="p12_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("💾 Save Draft Securely", key="p12_save_draft", use_container_width=True):
            st.session_state.form_data["review_confirm"] = review_confirm
            upload_result = sync_pending_uploads()
            if not upload_result.get("ok"):
                show_missing_fields(upload_result.get("errors", []), "Please fix the document upload issues before saving your draft:")
                return

            draft_result = autosave_draft()
            if draft_result and draft_result.get("ok"):
                st.success(f"Draft saved. Resume later with code `{st.session_state.draft_id}`.")
            else:
                st.warning("The form is still open, but the secure draft save did not complete.")
    with bcol3:
        if st.button("✅ Submit Application", key="p12_submit", use_container_width=True, type="primary"):
            if not review_confirm:
                st.error("Please confirm that you reviewed the application before submitting.")
            else:
                upload_result = sync_pending_uploads()
                if not upload_result.get("ok"):
                    show_missing_fields(upload_result.get("errors", []), "Please fix the document upload issues before submitting:")
                    return

                st.session_state.form_data["review_confirm"] = True
                st.session_state.form_data["final_submission_timestamp"] = datetime.now().isoformat()
                st.session_state.submitted = True
                st.rerun()


def render_submission_complete(submissions_dir: Path) -> None:
    st.session_state.current_page = 99

    if st.session_state.submission_artifacts is None:
        try:
            st.session_state.submission_artifacts = build_submission_artifacts()
        except Exception as exc:
            st.session_state.submission_save_error = f"Could not generate submission PDFs: {exc}"

    if st.session_state.submission_artifacts is not None and st.session_state.saved_submission_dir is None:
        try:
            saved_result = save_submission_bundle(submissions_dir, st.session_state.submission_artifacts)
            st.session_state.saved_submission_dir = saved_result.get("location_label")
            warnings = saved_result.get("warnings", [])
            if warnings:
                st.session_state.submission_save_notice = "\n".join(warnings)
        except Exception as exc:
            st.session_state.submission_save_error = f"Could not save submission files: {exc}"

    _attempt_submission_notification()

    st.balloons()
    st.success("### ✅ Application Submitted Successfully!")
    st.markdown(
        f"""
    Thank you, **{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}**!

    Your application to {COMPANY_NAME} has been received.
    A confirmation has been created with the following details:

    - **Submission Timestamp:** {st.session_state.form_data.get('final_submission_timestamp', datetime.now().isoformat())}
    - **Application Signature:** {st.session_state.form_data.get('sig_full_name', 'N/A')}
    """
    )

    if st.session_state.saved_submission_dir:
        st.info(f"A company copy was saved to: `{st.session_state.saved_submission_dir}`")
    elif st.session_state.submission_save_error:
        st.warning(st.session_state.submission_save_error)

    if st.session_state.submission_save_notice:
        st.warning(st.session_state.submission_save_notice)

    if st.session_state.get("submission_notification_status"):
        st.info(st.session_state.submission_notification_status)
    if st.session_state.get("submission_notification_error"):
        st.warning(f"Internal notification warning: {st.session_state.submission_notification_error}")

    st.caption(
        "This app stores the submission using the configured storage backend and offers manual downloads below. "
        "Notification emails, when configured, exclude attachments and sensitive SSN data."
    )

    st.markdown("---")
    st.subheader("Download Your Application PDF")

    try:
        pdf_bytes = (
            st.session_state.submission_artifacts["application_pdf"]
            if st.session_state.submission_artifacts
            else generate_application_pdf(
                st.session_state.form_data,
                st.session_state.employers,
                st.session_state.licenses,
                st.session_state.accidents,
                st.session_state.violations,
            )
        )
        st.download_button(
            label="📥 Download Application PDF",
            data=pdf_bytes,
            file_name=f"prestige_application_{st.session_state.form_data.get('last_name', 'driver')}_{date.today().isoformat()}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    except Exception as exc:
        st.error(f"PDF generation error: {exc}")

    st.markdown("---")
    st.subheader("Standalone Disclosure Documents")

    dcol1, dcol2, dcol3 = st.columns(3)
    with dcol1:
        try:
            fcra_pdf = (
                st.session_state.submission_artifacts["fcra_pdf"]
                if st.session_state.submission_artifacts
                else generate_fcra_pdf(st.session_state.form_data)
            )
            st.download_button(
                label="📥 FCRA Disclosure PDF",
                data=fcra_pdf,
                file_name="FCRA_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"FCRA PDF error: {exc}")

    with dcol2:
        try:
            psp_pdf = (
                st.session_state.submission_artifacts["psp_pdf"]
                if st.session_state.submission_artifacts
                else generate_psp_pdf(st.session_state.form_data)
            )
            st.download_button(
                label="📥 PSP Disclosure PDF",
                data=psp_pdf,
                file_name="PSP_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"PSP PDF error: {exc}")

    with dcol3:
        try:
            ch_pdf = (
                st.session_state.submission_artifacts["clearinghouse_pdf"]
                if st.session_state.submission_artifacts
                else generate_clearinghouse_pdf(st.session_state.form_data)
            )
            st.download_button(
                label="📥 Clearinghouse Release PDF",
                data=ch_pdf,
                file_name="Clearinghouse_Release_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"Clearinghouse PDF error: {exc}")

    if st.session_state.submission_artifacts and st.session_state.submission_artifacts.get("california_pdf"):
        try:
            st.download_button(
                label="📥 California Disclosure PDF",
                data=st.session_state.submission_artifacts["california_pdf"],
                file_name="California_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"California PDF error: {exc}")

    if st.session_state.get("uploaded_documents"):
        st.markdown("---")
        st.subheader("Saved Supporting Documents")
        for document in st.session_state.get("uploaded_documents", []):
            size_kb = max(1, int(document.get("size_bytes", 0) / 1024))
            st.markdown(f"- `{document.get('file_name', 'document')}` ({size_kb} KB)")

    if st.button("🔄 Start New Application"):
        reset_application_state()
        st.rerun()
