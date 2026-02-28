import streamlit as st
import requests
import time

st.set_page_config(page_title="AI Resume Optimizer", layout="wide")

st.title("🚀 AI Resume Optimizer")

# Sidebar
st.sidebar.header("LLM Configuration")
provider = st.sidebar.selectbox("Select LLM Provider", ["OpenAI", "Anthropic", "Groq", "Google AI Studio"])
api_key = st.sidebar.text_input("API Key", type="password")

if not api_key:
    st.sidebar.warning("⚠️ Please provide an API key to proceed.")

# Main Content
st.header("1. Upload Resume & Job Details")
uploaded_file = st.file_uploader("Choose a PDF resume", type="pdf")
job_description = st.text_area("Job Description", height=200)

if st.button("Run Optimizer"):
    if not api_key:
        st.error("API Key is missing!")
    elif not uploaded_file:
        st.error("Please upload a resume!")
    elif not job_description:
        st.error("Please provide a job description!")
    else:
        # Prepare payload
        files = {"file": uploaded_file.getvalue()}
        data = {
            "job_description": job_description,
            "llm_provider": provider,
            "api_key": api_key
        }

        with st.spinner("Initializing optimization task..."):
            try:
                # Use multipart form data for file + JSON data
                # Django Ninja can handle this if defined correctly, but let's be careful.
                # Actually, our API expects `payload: OptimizeRequest` which is typically from body,
                # and `file: UploadedFile` which is from files.
                # In Ninja, if we have both, it expects form-data.

                response = requests.post(
                    "http://localhost:8000/api/resume/optimize",
                    data=data,
                    files={"file": (uploaded_file.name, uploaded_file.getvalue(), "application/pdf")}
                )

                if response.status_code == 200:
                    result = response.json()
                    resume_id = result["resume_id"]
                    st.success(f"Task started! Resume ID: {resume_id}")

                    # Polling
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    log_area = st.expander("Agent Thoughts", expanded=True)

                    while True:
                        status_res = requests.get(f"http://localhost:8000/api/resume/status/{resume_id}")
                        if status_res.status_code == 200:
                            status_data = status_res.json()
                            status = status_data["status"]
                            status_text.text(f"Current Status: {status}")

                            # Update logs
                            with log_area:
                                for log in status_data["logs"]:
                                    st.write(f"**{log['step']}**: {log['thought']}")

                            if status == "completed":
                                progress_bar.progress(100)
                                st.balloons()
                                st.header("✅ Optimized Resume")
                                st.markdown(status_data["optimized_content"])

                                col1, col2 = st.columns(2)
                                col1.metric("ATS Score", status_data["ats_score"])
                                col2.metric("Recruiter Score", status_data["recruiter_score"])
                                break
                            elif "failed" in status:
                                st.error(f"Task failed: {status}")
                                break
                            else:
                                progress_bar.progress(50) # Indeterminate mostly

                        time.sleep(2)
                else:
                    st.error(f"Error starting task: {response.text}")
            except Exception as e:
                st.error(f"Connection error: {str(e)}")
