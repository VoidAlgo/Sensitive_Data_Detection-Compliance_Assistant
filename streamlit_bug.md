i gave a prompt on claude on chrome extension

"i have deployed in https://sensitivedatadetection-complianceassistant.streamlit.app/ and done the project in https://github.com/Makilesh/Sensitive_Data_Detection-Compliance_Assistant/tree/main

it is working perfectly on my local, but in my deployed place, there are few bugs i am getting, like each time new problems, like files not getting shared, or files not getting submitted, or when asked to reveal the data in the findings tab, it is going back to normal and not doing the work it is supposed to do. and when handling 2 or more files, there is a option on the left sidebar to choose between them, and that is getting disappeared when i wanted to switch. understand the problem and solve it out"


and got this as the result from the chat, understand and solve the bugs


"I've now read your entire `app.py` and I understand all four bugs you're seeing. They're all classic **Streamlit rerun / `session_state`** problems, not logic bugs in your `src/` code — which is exactly why it "works on local" intermittently but breaks on the deployed cloud (Streamlit Cloud reruns more aggressively and sometimes evicts widget state on file-uploader interactions).

Here's the diagnosis and the fix for each.

## The root cause

Streamlit re-runs `main()` top-to-bottom on **every** widget interaction. Two design choices in your code fight against that:

1. **`submit_clicked = st.button(...)` is only `True` on the one rerun where the button was pressed.** Any interaction afterward (toggling a checkbox, switching documents, typing in chat) reruns the script with `submit_clicked = False`, so the whole ingest block is skipped. You rely on `st.session_state["documents"]` persisting — which it does — *but* the `st.file_uploader` widget state and the caches interact badly, causing the symptoms below.

2. **Nothing has a stable `key=`.** Widgets without a `key` are re-created fresh and can lose their value across reruns, especially on Streamlit Cloud.

## Bug-by-bug fix

**1. "Reveal raw values" checkbox reverts / findings tab goes back to normal**
In `render_findings`, your checkbox has no `key`. When you tick it, Streamlit reruns; because there's no stable key and the findings are recomputed/re-rendered, the checkbox value is lost and it snaps back to `False`. Give it a per-document key:

```python
reveal = st.checkbox(
    "Reveal raw values (handle with care)",
    value=False,
    key=f"reveal_{findings[0].doc_id if findings else 'none'}",  # stable per doc
)
```
(Better: pass `doc_id` into `render_findings(findings, doc_id)` and use `key=f"reveal_{doc_id}"`.)

**2. The document switcher (selectbox) disappears when you have 2+ files**
The `st.selectbox("Active document", ...)` is rendered **only inside `if documents:`**, and `documents` is the local variable. On a rerun triggered by *switching* the document, `submit_clicked` is `False`, so `documents` is read from `st.session_state` — that's fine — but the selectbox has **no `key`**, so its selected value resets, and combined with the caches being rebuilt the sidebar section can vanish for a frame. Fix: give the selectbox a stable key and read documents from session_state unconditionally:

```python
active_id = st.selectbox(
    "Active document",
    options=list(documents.keys()),
    format_func=lambda i: labels[i],
    key="active_document_id",   # <-- persists selection across reruns
)
```

**3. Files "not getting submitted" / "not getting shared"**
`st.button` returning `True` for only one rerun means the ingest result must be committed to `session_state` *before* anything else can rerun. You do write `st.session_state["documents"] = new_documents`, but you build `new_documents` from `uploaded_files`, which can be empty on a stale rerun. Guard it and add `key="uploader"` so the uploaded files persist:

```python
uploaded_files = st.file_uploader(..., key="uploader")
submit_clicked = st.button("Submit / Analyze Uploaded Files", type="primary", key="submit_btn")

if submit_clicked and uploaded_files:   # <-- don't wipe docs on an empty submit
    ...
```

**4. General flakiness (each time a new problem)**
This is the tell-tale sign of unkeyed widgets on Streamlit Cloud. The robust fix is: **add a `key=` to every interactive widget** (uploader, submit button, both checkboxes, the selectbox, the summary button, `st.chat_input`), and always read `documents = st.session_state.get("documents", {})` rather than relying on the local variable.

## Recommended minimal patch to `app.py`

- `st.file_uploader(..., key="uploader")`
- `st.button("Submit / Analyze Uploaded Files", ..., key="submit_btn")` and change the guard to `if submit_clicked and uploaded_files:`
- `st.selectbox("Active document", ..., key="active_document_id")`
- In `render_findings`: pass `doc_id` in and use `key=f"reveal_{doc_id}"`
- In `render_chat`: `st.checkbox(..., key=f"corpus_{document.doc_id}")` and `st.chat_input(..., key=f"chat_{document.doc_id}")`
- In `render_summary`: `st.button("Generate compliance summary", ..., key=f"summary_{document.doc_id}")`

A quick note on two of your phrasings: you mentioned files "not getting **shared**." If by that you mean a Streamlit feature that changes who can access documents/permissions, I can't implement permission/sharing changes for you — but based on your description (corpus mode across multiple uploaded files) this is really the multi-file **state** issue above, which the keys fix.

Would you like me to:
- **(a)** open the deployed app and reproduce each bug live (upload a test file, toggle reveal, switch documents) to confirm before you change code, and/or
- **(b)** produce a complete, ready-to-paste corrected `app.py` with all the keys added?

I'd recommend (b) — I can write the full fixed file for you right now. Just confirm and I'll generate it."