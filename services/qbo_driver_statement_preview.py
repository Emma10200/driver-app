from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

try:
    from st_aggrid import AgGrid, DataReturnMode, GridOptionsBuilder, GridUpdateMode
except ImportError:  # pragma: no cover - deployed fallback when optional component is unavailable
    AgGrid = None
    DataReturnMode = None
    GridOptionsBuilder = None
    GridUpdateMode = None
    _AGGRID_AVAILABLE = False
else:
    _AGGRID_AVAILABLE = True

from qbo.models import PreviewResult

QBO_DRIVER_EDIT_NOTICE_KEY = "qbo_driver_preview_edit_notice"
QBO_DRIVER_PENDING_KEY = "qbo_driver_preview_pending"
QBO_DRIVER_RESET_KEY = "qbo_driver_preview_reset_counter"
QBO_DRIVER_UNCHECK_KEY = "qbo_driver_preview_uncheck_all"
QBO_DRIVER_SELECTION_KEY = "qbo_driver_preview_selected_refs"

_DRIVER_PREVIEW_ORIGINAL_KEYS = (
    "_original_doc_number",
    "_original_txn_date",
    "_original_vendor",
    "_original_division",
    "_original_realm_id",
    "_original_bank_account",
    "_original_bank_account_id",
    "_original_line_amount",
    "_original_expense_account",
    "_original_line_description",
    "_original_detail_type",
)


def clear_driver_statement_preview_state(*, edit_notice_only: bool = False) -> None:
    """Clear driver-statement Streamlit state while preserving the public key strings."""
    st.session_state.pop(QBO_DRIVER_EDIT_NOTICE_KEY, None)
    if edit_notice_only:
        return
    st.session_state.pop(QBO_DRIVER_PENDING_KEY, None)
    st.session_state.pop(QBO_DRIVER_RESET_KEY, None)
    st.session_state.pop(QBO_DRIVER_UNCHECK_KEY, None)
    st.session_state.pop(QBO_DRIVER_SELECTION_KEY, None)


def _editable_amount(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch in {".", "-"})
    try:
        return float(cleaned)
    except ValueError:
        return default


def _editable_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _editor_records(value: Any) -> list[dict[str, Any]]:
    try:
        records = value.to_dict("records")  # type: ignore[attr-defined]
    except AttributeError:
        records = list(value or [])
    return [dict(row) for row in records if isinstance(row, Mapping)]


def _draft_amount(draft: dict[str, Any]) -> float:
    total = 0.0
    for line in draft.get("Line") or []:
        try:
            total += float((line or {}).get("Amount") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _table_records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
        except TypeError:
            records = []
        return [dict(row) for row in records if isinstance(row, Mapping)]
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    return []


def _driver_statement_preview_rows_from_drafts(
    drafts: list[dict[str, Any]],
    *,
    include_edit_keys: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for draft_index, draft in enumerate(drafts or []):
        bank_ref = draft.get("AccountRef") or {}
        total = _draft_amount(draft)
        lines = draft.get("Line") or []
        if not lines:
            row = {
                "Post?": True,
                "QBO Txn Type": "Check",
                "Doc #": draft.get("DocNumber"),
                "Txn Date": draft.get("TxnDate"),
                "Payment Type": draft.get("PaymentType") or "Check",
                "Vendor": draft.get("_tempVendorName"),
                "Division": draft.get("_division") or "",
                "Realm ID": draft.get("_realmId") or "",
                "Bank Account": bank_ref.get("name") or "",
                "Bank Account ID": bank_ref.get("value") or draft.get("_bankAccountId") or "",
                "Check Total": total,
            }
            if include_edit_keys:
                _attach_driver_statement_edit_keys(row, draft_index=draft_index, line_index=-1)
            rows.append(row)
            continue

        for line_index, line in enumerate(lines):
            detail = (line or {}).get("AccountBasedExpenseLineDetail") or {}
            account_ref = detail.get("AccountRef") or {}
            row = {
                "Post?": True,
                "QBO Txn Type": "Check",
                "Doc #": draft.get("DocNumber"),
                "Txn Date": draft.get("TxnDate"),
                "Payment Type": draft.get("PaymentType") or "Check",
                "Vendor": draft.get("_tempVendorName"),
                "Division": draft.get("_division") or "",
                "Realm ID": draft.get("_realmId") or "",
                "Bank Account": bank_ref.get("name") or "",
                "Bank Account ID": bank_ref.get("value") or draft.get("_bankAccountId") or "",
                "Check Total": total,
                "Line #": line_index + 1,
                "Line Amount": (line or {}).get("Amount") or 0,
                "Expense Account": (line or {}).get("_tempAccountName") or account_ref.get("name") or "",
                "Line Description": (line or {}).get("Description") or "",
                "Detail Type": (line or {}).get("DetailType") or "",
            }
            if include_edit_keys:
                _attach_driver_statement_edit_keys(row, draft_index=draft_index, line_index=line_index)
            rows.append(row)
    return rows


def _attach_driver_statement_edit_keys(row: dict[str, Any], *, draft_index: int, line_index: int) -> None:
    row["_draft_index"] = draft_index
    row["_line_index"] = line_index
    row["_original_doc_number"] = _editable_text(row.get("Doc #"))
    row["_original_txn_date"] = _editable_text(row.get("Txn Date"))
    row["_original_vendor"] = _editable_text(row.get("Vendor"))
    row["_original_division"] = _editable_text(row.get("Division"))
    row["_original_realm_id"] = _editable_text(row.get("Realm ID"))
    row["_original_bank_account"] = _editable_text(row.get("Bank Account"))
    row["_original_bank_account_id"] = _editable_text(row.get("Bank Account ID"))
    row["_original_line_amount"] = _editable_amount(row.get("Line Amount"))
    row["_original_expense_account"] = _editable_text(row.get("Expense Account"))
    row["_original_line_description"] = _editable_text(row.get("Line Description"))
    row["_original_detail_type"] = _editable_text(row.get("Detail Type"))


def _driver_statement_cell_changed(row: dict[str, Any], field: str, original_key: str, *, amount: bool = False) -> bool:
    if original_key not in row:
        return True
    if amount:
        return _editable_amount(row.get(field)) != _editable_amount(row.get(original_key))
    return _editable_text(row.get(field)) != _editable_text(row.get(original_key))


def _driver_statement_row_ref(row: dict[str, Any]) -> tuple[int, int] | None:
    try:
        draft_index = int(row.get("_draft_index"))
        line_index = int(row.get("_line_index", -1))
    except (TypeError, ValueError):
        return None
    if draft_index < 0:
        return None
    return draft_index, line_index


def _driver_statement_existing_refs(drafts: list[dict[str, Any]]) -> set[tuple[int, int]]:
    refs: set[tuple[int, int]] = set()
    for draft_index, draft in enumerate(drafts or []):
        lines = draft.get("Line") or []
        if not lines:
            refs.add((draft_index, -1))
            continue
        refs.update((draft_index, line_index) for line_index in range(len(lines)))
    return refs


def _driver_statement_row_should_post(row: dict[str, Any]) -> bool:
    value = row.get("Post?", True)
    if isinstance(value, bool):
        return value
    return _editable_text(value).lower() not in {"", "0", "false", "no", "n", "off"}


def _remove_driver_statement_refs(drafts: list[dict[str, Any]], deleted_refs: set[tuple[int, int]]) -> int:
    if not deleted_refs:
        return 0
    removed = 0
    drafts_to_delete: set[int] = set()
    lines_by_draft: dict[int, set[int]] = {}
    for draft_index, line_index in deleted_refs:
        if line_index < 0:
            drafts_to_delete.add(draft_index)
        else:
            lines_by_draft.setdefault(draft_index, set()).add(line_index)

    for draft_index, line_indices in lines_by_draft.items():
        if draft_index < 0 or draft_index >= len(drafts):
            continue
        lines = drafts[draft_index].get("Line") or []
        for line_index in sorted(line_indices, reverse=True):
            if 0 <= line_index < len(lines):
                del lines[line_index]
                removed += 1
        drafts[draft_index]["Line"] = lines
        if not lines:
            drafts_to_delete.add(draft_index)

    for draft_index in sorted(drafts_to_delete, reverse=True):
        if 0 <= draft_index < len(drafts):
            if draft_index not in lines_by_draft:
                removed += 1
            del drafts[draft_index]
    return removed


def _apply_driver_statement_preview_edits(preview: PreviewResult, edited_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Apply edited/deleted driver-statement preview rows back to posted QBO check drafts."""
    if preview.template_type != "driver_statements":
        return {"fields": 0, "removed": 0}

    drafts = preview.drafts or []
    existing_refs = _driver_statement_existing_refs(drafts)
    seen_refs: set[tuple[int, int]] = set()
    kept_refs: set[tuple[int, int]] = set()
    rows_to_apply: list[dict[str, Any]] = []
    for row in edited_rows:
        ref = _driver_statement_row_ref(row)
        if ref is None or ref not in existing_refs:
            continue
        seen_refs.add(ref)
        if _driver_statement_row_should_post(row):
            kept_refs.add(ref)
            rows_to_apply.append(row)

    if edited_rows and not seen_refs:
        return {"fields": 0, "removed": 0}

    deleted_refs = existing_refs - kept_refs
    changed = 0
    for row in rows_to_apply:
        row_ref = _driver_statement_row_ref(row)
        if row_ref is None:
            continue
        draft_index, line_index = row_ref
        if draft_index < 0 or draft_index >= len(drafts):
            continue

        draft = drafts[draft_index]
        draft_field_map = (
            ("Doc #", "_original_doc_number", "DocNumber"),
            ("Txn Date", "_original_txn_date", "TxnDate"),
            ("Vendor", "_original_vendor", "_tempVendorName"),
            ("Division", "_original_division", "_division"),
            ("Realm ID", "_original_realm_id", "_realmId"),
        )
        for display_key, original_key, draft_key in draft_field_map:
            if not _driver_statement_cell_changed(row, display_key, original_key):
                continue
            new_value = _editable_text(row.get(display_key))
            if _editable_text(draft.get(draft_key)) != new_value:
                draft[draft_key] = new_value
                changed += 1

        bank_name_changed = _driver_statement_cell_changed(row, "Bank Account", "_original_bank_account")
        bank_id_changed = _driver_statement_cell_changed(row, "Bank Account ID", "_original_bank_account_id")
        if bank_name_changed or bank_id_changed:
            bank_ref = draft.setdefault("AccountRef", {})
            old_bank_id = _editable_text(bank_ref.get("value") or draft.get("_bankAccountId"))
            new_bank_name = _editable_text(row.get("Bank Account"))
            new_bank_id = _editable_text(row.get("Bank Account ID"))
            if bank_name_changed and not bank_id_changed and new_bank_id == old_bank_id:
                new_bank_id = ""
            if _editable_text(bank_ref.get("name")) != new_bank_name:
                bank_ref["name"] = new_bank_name
                changed += 1
            if new_bank_id:
                if _editable_text(bank_ref.get("value")) != new_bank_id:
                    bank_ref["value"] = new_bank_id
                    changed += 1
            elif bank_ref.pop("value", None) is not None:
                changed += 1
            if _editable_text(draft.get("_bankAccountId")) != new_bank_id:
                draft["_bankAccountId"] = new_bank_id
                changed += 1

        lines = draft.get("Line") or []
        if line_index < 0 or line_index >= len(lines):
            continue
        line = lines[line_index]
        if not isinstance(line, dict):
            continue

        if _driver_statement_cell_changed(row, "Line Amount", "_original_line_amount", amount=True):
            new_amount = _editable_amount(row.get("Line Amount"), _editable_amount(line.get("Amount")))
            if _editable_amount(line.get("Amount")) != new_amount:
                line["Amount"] = new_amount
                changed += 1
        if _driver_statement_cell_changed(row, "Line Description", "_original_line_description"):
            new_description = _editable_text(row.get("Line Description"))
            if _editable_text(line.get("Description")) != new_description:
                line["Description"] = new_description
                changed += 1
        if _driver_statement_cell_changed(row, "Detail Type", "_original_detail_type"):
            new_detail_type = _editable_text(row.get("Detail Type")) or "AccountBasedExpenseLineDetail"
            if _editable_text(line.get("DetailType")) != new_detail_type:
                line["DetailType"] = new_detail_type
                changed += 1
        if _driver_statement_cell_changed(row, "Expense Account", "_original_expense_account"):
            new_account = _editable_text(row.get("Expense Account"))
            detail = line.setdefault("AccountBasedExpenseLineDetail", {"AccountRef": {}})
            ref = detail.setdefault("AccountRef", {})
            if _editable_text(ref.get("name")) != new_account:
                ref["name"] = new_account
                changed += 1
            if ref.pop("value", None) is not None:
                changed += 1
            if _editable_text(line.get("_tempAccountName")) != new_account:
                line["_tempAccountName"] = new_account
                changed += 1

    removed = _remove_driver_statement_refs(drafts, deleted_refs)
    if changed or removed:
        preview.rows = _driver_statement_preview_rows_from_drafts(drafts)
        preview.count = len(drafts)
    return {"fields": changed, "removed": removed}


def _render_editable_driver_statement_preview(preview: PreviewResult) -> None:
    rows = _driver_statement_preview_rows_from_drafts(preview.drafts or [], include_edit_keys=True)
    if not rows:
        st.dataframe(preview.rows, use_container_width=True, hide_index=True)
        _set_driver_pending(preview.source_hash, None)
        _set_driver_uncheck_all(preview.source_hash, False)
        _set_driver_selected_refs(preview.source_hash, None)
        return

    if _driver_uncheck_all_pending(preview.source_hash):
        _set_driver_selected_refs(preview.source_hash, set())
        _set_driver_uncheck_all(preview.source_hash, False)

    notice = _driver_edit_notice(preview.source_hash)
    if notice:
        parts = []
        if int(notice.get("fields") or 0):
            parts.append(f"{notice.get('fields')} field(s) saved")
        if int(notice.get("removed") or 0):
            parts.append(f"{notice.get('removed')} row(s) removed")
        st.success(
            "✅ Last confirmed edit — "
            f"{', '.join(parts) or 'changes saved'} at {notice.get('time', '')}. "
            "The rows below are exactly what will post to QBO."
        )

    reset_counter = int(st.session_state.get(QBO_DRIVER_RESET_KEY, 0) or 0)
    editor_key = f"qbo_full_preview_editor_{preview.source_hash}_{reset_counter}"

    prior_pending = _driver_pending_for(preview) or {}
    prior_total = int(prior_pending.get("fields") or 0) + int(prior_pending.get("removed") or 0)
    confirm_col, discard_col, uncheck_col, info_col = st.columns([0.18, 0.18, 0.16, 0.48])
    with confirm_col:
        confirm_clicked = st.button(
            "✅ Confirm changes",
            type="primary",
            disabled=prior_total == 0,
            use_container_width=True,
            key=f"qbo_confirm_driver_edits_{preview.source_hash}",
            help="Apply your pending edits/unchecks to the post payload.",
        )
    with discard_col:
        discard_clicked = st.button(
            "↩️ Discard changes",
            disabled=prior_total == 0,
            use_container_width=True,
            key=f"qbo_discard_driver_edits_{preview.source_hash}",
            help="Undo every edit/uncheck since the last confirm. Original rows come back.",
        )
    with uncheck_col:
        uncheck_all_clicked = st.button(
            "☐ Uncheck all",
            use_container_width=True,
            key=f"qbo_uncheck_all_driver_{preview.source_hash}",
            help="Clear every Post? checkbox. Re-check only the rows you want, then confirm.",
        )
    with info_col:
        with st.popover("ℹ️ How this preview works", use_container_width=True):
            st.markdown(
                "- Rows are **checked to post** by default; uncheck the ones you want to skip.\n"
                "- **Shift+Click** a row/checkbox to select or clear a whole range.\n"
                "- Use the **Post? header checkbox** or **☐ Uncheck all** for all rows at once.\n"
                "- Edits/unchecks only apply after **Confirm changes**; **Discard changes** wipes pending edits."
                + ("" if _AGGRID_AVAILABLE else
                   "\n- _Enhanced grid not installed here — using safe fallback editor; same buttons still work._")
            )

    if _AGGRID_AVAILABLE:
        edited_records = _render_driver_statement_aggrid(rows, preview.source_hash, editor_key)
    else:
        edited_records = _render_driver_statement_streamlit_fallback(rows, preview.source_hash, editor_key)
    pending = _pending_driver_statement_changes(preview, edited_records)
    pending_total = int(pending.get("fields") or 0) + int(pending.get("removed") or 0)
    _set_driver_pending(preview.source_hash, pending if pending_total else None)

    if pending_total:
        pieces = []
        if pending.get("fields"):
            pieces.append(f"{pending['fields']} field change(s)")
        if pending.get("removed"):
            pieces.append(f"{pending['removed']} row(s) to remove")
        st.caption(
            "Pending: "
            + ", ".join(pieces)
            + ". Click **Confirm changes** above to lock them in, or **Discard changes** to undo."
        )
    else:
        st.caption("No pending changes. Posting will use the rows shown above.")

    if uncheck_all_clicked:
        _set_driver_uncheck_all(preview.source_hash, True)
        st.session_state[QBO_DRIVER_RESET_KEY] = reset_counter + 1
        st.rerun()

    if discard_clicked:
        st.session_state[QBO_DRIVER_RESET_KEY] = reset_counter + 1
        _set_driver_pending(preview.source_hash, None)
        _set_driver_uncheck_all(preview.source_hash, False)
        _set_driver_selected_refs(preview.source_hash, None)
        st.rerun()

    if confirm_clicked:
        result = _apply_driver_statement_preview_edits(preview, edited_records)
        if result.get("fields") or result.get("removed"):
            _remember_driver_edit_notice(preview.source_hash, result)
        st.session_state[QBO_DRIVER_RESET_KEY] = reset_counter + 1
        _set_driver_pending(preview.source_hash, None)
        _set_driver_uncheck_all(preview.source_hash, False)
        _set_driver_selected_refs(preview.source_hash, None)
        st.rerun()


def _render_driver_statement_aggrid(
    rows: list[dict[str, Any]], source_hash: str, editor_key: str
) -> list[dict[str, Any]]:
    if not _AGGRID_AVAILABLE or AgGrid is None or DataReturnMode is None or GridOptionsBuilder is None or GridUpdateMode is None:
        return _render_driver_statement_streamlit_fallback(rows, source_hash, editor_key)

    grid_rows = [{"Post": "", **row} for row in rows]
    display_df = pd.DataFrame(grid_rows)
    selected_refs = _driver_selected_refs(source_hash)
    if selected_refs is None:
        pre_selected_rows = list(range(len(rows)))
    else:
        pre_selected_rows = [
            index for index, row in enumerate(rows) if _driver_statement_row_ref(row) in selected_refs
        ]

    grid_options_builder = GridOptionsBuilder.from_dataframe(display_df)
    grid_options_builder.configure_default_column(
        editable=True,
        filter=True,
        resizable=True,
        sortable=True,
        wrapText=True,
    )
    grid_options_builder.configure_selection(
        selection_mode="multiple",
        use_checkbox=True,
        header_checkbox=True,
        header_checkbox_filtered_only=False,
        pre_selected_rows=pre_selected_rows,
        rowMultiSelectWithClick=True,
        suppressRowDeselection=False,
    )
    # streamlit-aggrid forces suppressRowClickSelection=True whenever checkbox selection is enabled.
    # Override it so normal row clicks and Shift+Click range selection work with the mouse.
    grid_options_builder.configure_grid_options(
        rowMultiSelectWithClick=True,
        suppressRowClickSelection=False,
    )
    for column in ("Post?", "_draft_index", "_line_index", *_DRIVER_PREVIEW_ORIGINAL_KEYS):
        if column in display_df.columns:
            grid_options_builder.configure_column(column, hide=True)
    for column in ("Post", "QBO Txn Type", "Payment Type", "Check Total", "Line #", "Detail Type"):
        if column in display_df.columns:
            grid_options_builder.configure_column(column, editable=False)
    if "Post" in display_df.columns:
        grid_options_builder.configure_column(
            "Post",
            headerName="Post?",
            width=90,
            pinned="left",
            editable=False,
            checkboxSelection=True,
            headerCheckboxSelection=True,
        )
    grid_response = AgGrid(
        display_df,
        gridOptions=grid_options_builder.build(),
        height=min(720, max(280, 36 * (len(rows) + 2))),
        data_return_mode=DataReturnMode.AS_INPUT,
        update_mode=GridUpdateMode.MODEL_CHANGED,
        allow_unsafe_jscode=False,
        theme="streamlit",
        key=editor_key,
        show_search=False,
        show_download_button=False,
        use_json_serialization=True,
    )

    raw_selected_rows = getattr(grid_response, "selected_rows", None)
    selected_records = _table_records(raw_selected_rows)
    if raw_selected_rows is None:
        if selected_refs is None:
            active_selected_refs = {
                ref for row in rows if (ref := _driver_statement_row_ref(row)) is not None
            }
        else:
            active_selected_refs = selected_refs
    else:
        active_selected_refs = {
            ref for row in selected_records if (ref := _driver_statement_row_ref(row)) is not None
        }
    _set_driver_selected_refs(source_hash, active_selected_refs)

    edited_records = _table_records(getattr(grid_response, "data", None)) or grid_rows
    normalized_records: list[dict[str, Any]] = []
    for row in edited_records:
        normalized_row = dict(row)
        normalized_row.pop("Post", None)
        ref = _driver_statement_row_ref(normalized_row)
        normalized_row["Post?"] = ref in active_selected_refs if ref is not None else False
        normalized_records.append(normalized_row)
    return normalized_records


def _render_driver_statement_streamlit_fallback(
    rows: list[dict[str, Any]], source_hash: str, editor_key: str
) -> list[dict[str, Any]]:
    selected_refs = _driver_selected_refs(source_hash)
    fallback_rows: list[dict[str, Any]] = []
    for row in rows:
        fallback_row = dict(row)
        ref = _driver_statement_row_ref(fallback_row)
        fallback_row["Post?"] = True if selected_refs is None else ref in selected_refs
        fallback_rows.append(fallback_row)

    hidden_columns = {"_draft_index": None, "_line_index": None}
    hidden_columns.update({key: None for key in _DRIVER_PREVIEW_ORIGINAL_KEYS})
    edited = st.data_editor(
        fallback_rows,
        key=f"{editor_key}_fallback",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            **hidden_columns,
            "Post?": st.column_config.CheckboxColumn(
                "Post?",
                help="Keep checked to post this row. Uncheck to remove it from this import.",
                required=True,
            ),
            "QBO Txn Type": st.column_config.TextColumn(disabled=True),
            "Doc #": st.column_config.TextColumn("Doc #", help="Check number / document number to post."),
            "Txn Date": st.column_config.TextColumn("Txn Date", help="Check date, usually YYYY-MM-DD."),
            "Payment Type": st.column_config.TextColumn(disabled=True),
            "Vendor": st.column_config.TextColumn("Vendor", help="Vendor name to look up in QuickBooks."),
            "Division": st.column_config.TextColumn("Division"),
            "Realm ID": st.column_config.TextColumn("Realm ID"),
            "Bank Account": st.column_config.TextColumn("Bank Account"),
            "Bank Account ID": st.column_config.TextColumn("Bank Account ID"),
            "Check Total": st.column_config.NumberColumn(disabled=True, format="$ %.2f"),
            "Line #": st.column_config.NumberColumn(disabled=True),
            "Line Amount": st.column_config.NumberColumn("Line Amount", format="$ %.2f"),
            "Expense Account": st.column_config.TextColumn(
                "Expense Account",
                help="Type the exact QBO account name to use for this line.",
            ),
            "Line Description": st.column_config.TextColumn("Line Description", width="large"),
            "Detail Type": st.column_config.TextColumn(disabled=True),
        },
    )
    edited_records = _editor_records(edited)
    active_selected_refs = {
        ref
        for row in edited_records
        if _driver_statement_row_should_post(row) and (ref := _driver_statement_row_ref(row)) is not None
    }
    _set_driver_selected_refs(source_hash, active_selected_refs)
    return edited_records


def _pending_driver_statement_changes(
    preview: PreviewResult, edited_rows: list[dict[str, Any]]
) -> dict[str, int]:
    """Return field/row change counts without mutating the actual preview."""
    snapshot = copy.deepcopy(preview)
    return _apply_driver_statement_preview_edits(snapshot, edited_rows)


def _set_driver_pending(source_hash: str, pending: dict[str, int] | None) -> None:
    store = st.session_state.setdefault(QBO_DRIVER_PENDING_KEY, {})
    if not isinstance(store, dict):
        store = {}
        st.session_state[QBO_DRIVER_PENDING_KEY] = store
    if pending:
        store[source_hash] = {
            "fields": int(pending.get("fields") or 0),
            "removed": int(pending.get("removed") or 0),
        }
    else:
        store.pop(source_hash, None)


def _driver_selected_refs(source_hash: str) -> set[tuple[int, int]] | None:
    store = st.session_state.get(QBO_DRIVER_SELECTION_KEY)
    if not isinstance(store, dict) or source_hash not in store:
        return None
    refs: set[tuple[int, int]] = set()
    for item in store.get(source_hash) or []:
        try:
            draft_index, line_index = item
            refs.add((int(draft_index), int(line_index)))
        except (TypeError, ValueError):
            continue
    return refs


def _set_driver_selected_refs(source_hash: str, refs: set[tuple[int, int]] | None) -> None:
    store = st.session_state.setdefault(QBO_DRIVER_SELECTION_KEY, {})
    if not isinstance(store, dict):
        store = {}
        st.session_state[QBO_DRIVER_SELECTION_KEY] = store
    if refs is None:
        store.pop(source_hash, None)
    else:
        store[source_hash] = sorted((int(draft_index), int(line_index)) for draft_index, line_index in refs)


def _driver_pending_for(preview: Any) -> dict[str, int] | None:
    if not isinstance(preview, PreviewResult) or preview.template_type != "driver_statements":
        return None
    store = st.session_state.get(QBO_DRIVER_PENDING_KEY)
    if not isinstance(store, dict):
        return None
    pending = store.get(preview.source_hash)
    if not isinstance(pending, dict):
        return None
    if int(pending.get("fields") or 0) + int(pending.get("removed") or 0) == 0:
        return None
    return pending


def _driver_uncheck_all_pending(source_hash: str) -> bool:
    store = st.session_state.get(QBO_DRIVER_UNCHECK_KEY)
    return isinstance(store, dict) and bool(store.get(source_hash))


def _set_driver_uncheck_all(source_hash: str, value: bool) -> None:
    store = st.session_state.setdefault(QBO_DRIVER_UNCHECK_KEY, {})
    if not isinstance(store, dict):
        store = {}
        st.session_state[QBO_DRIVER_UNCHECK_KEY] = store
    if value:
        store[source_hash] = True
    else:
        store.pop(source_hash, None)


def _driver_edit_notice(source_hash: str) -> dict[str, Any] | None:
    notices = st.session_state.get(QBO_DRIVER_EDIT_NOTICE_KEY)
    if not isinstance(notices, dict):
        return None
    notice = notices.get(source_hash)
    return notice if isinstance(notices, dict) and isinstance(notice, dict) else None


def _remember_driver_edit_notice(source_hash: str, result: dict[str, int]) -> None:
    notices = st.session_state.setdefault(QBO_DRIVER_EDIT_NOTICE_KEY, {})
    if not isinstance(notices, dict):
        notices = {}
        st.session_state[QBO_DRIVER_EDIT_NOTICE_KEY] = notices
    notices[source_hash] = {
        "fields": int(result.get("fields") or 0),
        "removed": int(result.get("removed") or 0),
        "time": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
    }
