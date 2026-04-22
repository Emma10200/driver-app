"""Review/submit and submission-complete renderers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import streamlit as st

from pdf_generator import (
    generate_application_pdf,
    generate_clearinghouse_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
)
from runtime_context import get_active_company_profile, get_storage_namespace, is_test_mode_active
from services.document_service import render_supporting_documents_section, sync_pending_uploads
from services.error_log_service import log_application_error
from services.notification_service import send_internal_submission_notification
from services.submission_service import build_submission_artifacts, save_submission_bundle
from state import prev_page, reset_application_state
from submission_storage import get_submission_destination_summary
from ui.common import render_save_draft_button, show_missing_fields, show_user_error, summary_item


def _attempt_submission_notification() -> None:
    if st.session_state.get("submission_notification_sent"):
        return
    if st.session_state.get("submission_notification_status_code") == "disabled":
        return

    saved_submission_dir = st.session_state.get("saved_submission_dir")
    if not saved_submission_dir:
        return

    try:
        artifacts = st.session_state.submission_artifacts or {}
        notification_result = send_internal_submission_notification(
            form_data=st.session_state.form_data,
            submission_result={"location_label": saved_submission_dir},
            uploaded_documents=st.session_state.get("uploaded_documents", []),
            application_pdf=artifacts.get("application_pdf"),
            artifacts=artifacts,
        )
    except Exception as exc:  # noqa: BLE001 - never let notification break the success page
        st.session_state.submission_notification_status_code = "error"
        st.session_state.submission_notification_status = None
        st.session_state.submission_notification_error = str(exc)
        log_application_error(
            code="submission_notification_exception",
            user_message="Internal submission notification raised an exception.",
            technical_details=str(exc),
            severity="warning",
        )
        return

    status = notification_result.get("status")
    st.session_state.submission_notification_status_code = status

    if status == "sent":
        st.session_state.submission_notification_sent = True
        st.session_state.submission_notification_status = notification_result.get("message")
        st.session_state.submission_notification_error = None
    elif status == "disabled":
        st.session_state.submission_notification_status = notification_result.get("message")
        st.session_state.submission_notification_error = None
        log_application_error(
            code="submission_notification_disabled",
            user_message="Internal submission notification skipped (not configured).",
            technical_details=notification_result.get("message"),
            severity="warning",
        )
    else:
        st.session_state.submission_notification_status = None
        st.session_state.submission_notification_error = notification_result.get("message")
        log_application_error(
            code="submission_notification_failed",
            user_message="Internal submission notification failed.",
            technical_details=notification_result.get("message"),
            severity="warning",
        )


def render_review_submit_page(submissions_dir: Path) -> None:
    company = get_active_company_profile()
    st.subheader("🧾 Review & Submit")
    st.markdown(
        "Please review the summary of your application below. "
        "Use the **Back** button if you need to make any changes before submitting."
    )
    if is_test_mode_active():
        st.warning(
            "Safe test mode is active. Test submissions use fake data and are kept separate from real applications."
        )
    # Note: actual storage destination is logged internally; not shown to applicants.
    submission_destination = get_submission_destination_summary(
        submissions_dir,
        storage_namespace=get_storage_namespace(),
    )

    with st.expander("Personal Information", expanded=True):
        summary_item(
            "Applicant",
            f"{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}".strip(),
        )
        summary_item("Date of Birth", st.session_state.form_data.get("dob"))
        summary_item(
            "Address",
            f"{st.session_state.form_data.get('address', '')}, {st.session_state.form_data.get('city', '')}, "
            f"{st.session_state.form_data.get('state', '')} {st.session_state.form_data.get('zip_code', '')}".strip(", "),
        )
        summary_item("Primary Phone", st.session_state.form_data.get("primary_phone"))
        summary_item("Cell Phone / Text Number", st.session_state.form_data.get("cell_phone"))
        summary_item(
            "Mobile Carrier / Provider",
            st.session_state.form_data.get("mobile_carrier_other") or st.session_state.form_data.get("mobile_carrier"),
        )
        summary_item("Text Message Consent", st.session_state.form_data.get("text_consent"))
        summary_item("Email", st.session_state.form_data.get("email"))
        summary_item(
            "Emergency Contact",
            f"{st.session_state.form_data.get('emergency_name', '')} / {st.session_state.form_data.get('emergency_phone', '')}",
        )

    with st.expander("Company Questions & Experience"):
        summary_item("Position Applying For", st.session_state.form_data.get("position"))
        summary_item(
            "Preferred Office for Onboarding",
            st.session_state.form_data.get("preferred_office") or st.session_state.form_data.get("applying_location"),
        )
        summary_item(
            "Currently Employed/Contracted Elsewhere",
            st.session_state.form_data.get("currently_employed"),
        )
        summary_item("Previously Contracted Here", st.session_state.form_data.get("worked_here_before"))
        summary_item("Referral Source", st.session_state.form_data.get("referral_source"))
        summary_item("Employment History Entries", len(st.session_state.employers), default="0")

    with st.expander("Licenses & Endorsements"):
        summary_item("License Entries", len(st.session_state.licenses), default="0")
        summary_item("TWIC Card", st.session_state.form_data.get("twic_card"))
        if st.session_state.form_data.get("twic_card") == "Yes":
            summary_item("TWIC Expiration", st.session_state.form_data.get("twic_expiration"))
        summary_item("HazMat Endorsement", st.session_state.form_data.get("hazmat_endorsement"))
        if st.session_state.form_data.get("hazmat_endorsement") == "Yes":
            summary_item("HazMat Expiration", st.session_state.form_data.get("hazmat_expiration"))

    with st.expander("Education, Safety, and Records"):
        summary_item("Highest Grade Completed", st.session_state.form_data.get("highest_grade"))
        summary_item("Attended Trucking School", st.session_state.form_data.get("attended_trucking_school"))
        summary_item("Accidents Reported", len(st.session_state.accidents), default="0")
        summary_item("Violations Reported", len(st.session_state.violations), default="0")
        summary_item("Currently Disqualified", st.session_state.form_data.get("disq_391_15"))
        summary_item(
            "Suspended or Revoked License History",
            st.session_state.form_data.get("disq_suspended"),
        )
        convicted_which = st.session_state.form_data.get("disq_convicted_which") or []
        if st.session_state.form_data.get("disq_convicted") == "Yes" and convicted_which:
            summary_item("DOT Offense(s) Disclosed", "; ".join(convicted_which))
        summary_item("Supporting Documents Uploaded", len(st.session_state.get("uploaded_documents", [])), default="0")

    with st.expander("Disclosures & Acknowledgments"):
        summary_item("Drug and Alcohol Policy", st.session_state.form_data.get("drug_alcohol_cert"))
        summary_item("Applicant Certification", st.session_state.form_data.get("applicant_cert"))
        summary_item("FCRA Acknowledged", st.session_state.form_data.get("fcra_acknowledge"))
        if st.session_state.form_data.get("ca_applicable"):
            summary_item(
                "California Disclosure Acknowledged",
                st.session_state.form_data.get("ca_disclosure_acknowledge"),
            )
        else:
            summary_item("California Disclosure", "Not applicable")
        summary_item("Consumer Copy Requested", st.session_state.form_data.get("ca_copy"))
        summary_item("PSP Acknowledged", st.session_state.form_data.get("psp_acknowledge"))
        summary_item("Clearinghouse Acknowledged", st.session_state.form_data.get("clearinghouse_acknowledge"))
        summary_item(
            "Investigative Consumer Report Acknowledged",
            st.session_state.form_data.get("inv_consumer_report"),
        )

    render_supporting_documents_section()

    st.markdown("---")
    st.markdown("### What happens next")
    st.markdown(
        f"When you submit, your application is securely sent to {company.name} for review. "
        "You'll be able to download a copy of your application and disclosures from the confirmation page. "
        f"Our team will reach out to you using the contact information you provided."
    )

    review_confirm = st.checkbox(
        "I reviewed the information above and I am ready to submit this application.",
        value=st.session_state.form_data.get("review_confirm", False),
    )

    def _prepare_review_draft_save() -> bool:
        st.session_state.form_data["review_confirm"] = review_confirm
        upload_result = sync_pending_uploads()
        if not upload_result.get("ok"):
            show_missing_fields(
                upload_result.get("errors", []),
                "Please fix the document upload issues before saving your draft:",
            )
            return False
        return True

    bcol1, bcol2, bcol3 = st.columns(3)
    with bcol1:
        if st.button("← Back", key="p12_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        render_save_draft_button(
            "p12_save_draft",
            label="Save Draft",
            on_before_save=_prepare_review_draft_save,
        )
    with bcol3:
        if st.button("Submit Application", key="p12_submit", use_container_width=True, type="primary"):
            if not review_confirm:
                show_user_error(
                    "Please confirm that you reviewed the application before submitting.",
                    code="validation_review_confirm_required",
                    severity="warning",
                )
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
    try:
        _render_submission_complete_body(submissions_dir)
    except Exception as exc:  # noqa: BLE001 - applicant must always see a success state
        log_application_error(
            code="submission_complete_render_failed",
            user_message="Submission completed but the confirmation page hit an error.",
            technical_details=str(exc),
            severity="error",
        )
        st.success("### ✅ Application Submitted Successfully!")
        st.markdown(
            "Your application has been received. "
            "If you'd like a copy for your records, please contact us and we'll send one over."
        )


def _render_submission_complete_body(submissions_dir: Path) -> None:
    company = get_active_company_profile()
    st.session_state.current_page = 99

    if st.session_state.submission_artifacts is None:
        try:
            st.session_state.submission_artifacts = build_submission_artifacts()
        except Exception as exc:
            log_application_error(
                code="submission_pdf_generation_failed",
                user_message="Could not generate submission PDFs.",
                technical_details=str(exc),
            )
            st.session_state.submission_save_error = "We could not finish preparing your submission packet. Please try again."

    if st.session_state.submission_artifacts is not None and st.session_state.saved_submission_dir is None:
        try:
            saved_result = save_submission_bundle(submissions_dir, st.session_state.submission_artifacts)
            st.session_state.saved_submission_dir = saved_result.get("location_label")
            warnings = saved_result.get("warnings", [])
            if warnings:
                st.session_state.submission_save_notice = "\n".join(warnings)
        except Exception as exc:
            log_application_error(
                code="submission_persistence_failed",
                user_message="Could not save submission files.",
                technical_details=str(exc),
            )
            st.session_state.submission_save_error = "Your application could not be saved right now. Please try again shortly."

    _attempt_submission_notification()

    st.success("### ✅ Application Submitted Successfully!")
    st.markdown(
        f"""
    Thank you, **{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}**!

    Your application to {company.name} has been received. Our team will reach out to you using the contact information you provided.

    - **Submitted:** {st.session_state.form_data.get('final_submission_timestamp', datetime.now().isoformat())}
    - **Signed by:** {st.session_state.form_data.get('sig_full_name', 'N/A')}

    A copy of your application and disclosure documents is available for download below.
    """
    )

    if st.session_state.submission_save_error:
        st.warning(st.session_state.submission_save_error)

    if st.session_state.submission_save_notice:
        log_application_error(
            code="submission_persistence_warning",
            user_message="Submission completed with storage warnings.",
            technical_details=st.session_state.submission_save_notice,
            severity="warning",
        )
        st.warning("Your application was submitted, but an internal follow-up check is pending.")

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
            file_name=f"{company.slug}_application_{st.session_state.form_data.get('last_name', 'driver')}_{date.today().isoformat()}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    except Exception as exc:
        show_user_error(
            "We couldn't prepare the application PDF download right now.",
            code="download_application_pdf_failed",
            technical_details=str(exc),
        )

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
            show_user_error(
                "We couldn't prepare the FCRA disclosure PDF right now.",
                code="download_fcra_pdf_failed",
                technical_details=str(exc),
            )

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
            show_user_error(
                "We couldn't prepare the PSP disclosure PDF right now.",
                code="download_psp_pdf_failed",
                technical_details=str(exc),
            )

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
            show_user_error(
                "We couldn't prepare the Clearinghouse release PDF right now.",
                code="download_clearinghouse_pdf_failed",
                technical_details=str(exc),
            )

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
            show_user_error(
                "We couldn't prepare the California disclosure PDF right now.",
                code="download_california_pdf_failed",
                technical_details=str(exc),
            )

    if st.session_state.get("uploaded_documents"):
        st.markdown("---")
        st.subheader("Saved Supporting Documents")
        for document in st.session_state.get("uploaded_documents", []):
            size_kb = max(1, int(document.get("size_bytes", 0) / 1024))
            st.markdown(f"- `{document.get('file_name', 'document')}` ({size_kb} KB)")

    st.markdown("---")
    with st.expander("Need to start a new application?"):
        st.caption(
            "This will clear the current confirmation and start a fresh application from page 1. "
            "Only do this if you're submitting on behalf of a different applicant."
        )
        if st.button("Start New Application", key="start_new_application"):
            reset_application_state()
            st.rerun()
