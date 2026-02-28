from typing import TypedDict, List, Annotated
import operator
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END

# --- LLM Factory ---
def get_llm(provider: str, api_key: str):
    if provider == "OpenAI":
        return ChatOpenAI(model="gpt-4o", api_key=api_key)
    elif provider == "Anthropic":
        return ChatAnthropic(model="claude-3-5-sonnet-latest", api_key=api_key)
    elif provider == "Groq":
        return ChatGroq(model="llama3-70b-8192", api_key=api_key)
    elif provider == "Google AI Studio":
        return ChatGoogleGenerativeAI(model="gemini-1.5-pro", google_api_key=api_key)
    else:
        raise ValueError(f"Unsupported provider: {provider}")

# --- State Definition ---
class AgentState(TypedDict):
    resume_text: str
    job_description: str
    optimized_resume: str
    ats_score: int
    recruiter_score: int
    feedback: List[str]
    iteration_count: int
    llm: any

# --- Agent Nodes ---

def writer_node(state: AgentState):
    llm = state['llm']
    prompt = f"""
    You are an expert Resume Writer. Your task is to tailor the following resume to the job description provided.
    Ensure you highlight relevant skills and experiences without hallucinating any information.

    Resume:
    {state['resume_text']}

    Job Description:
    {state['job_description']}

    Previous Feedback:
    {", ".join(state['feedback'])}

    Optimized Resume:
    """
    response = llm.invoke([HumanMessage(content=prompt)])
    return {
        "optimized_resume": response.content,
        "iteration_count": state['iteration_count'] + 1
    }

def ats_judge_node(state: AgentState):
    llm = state['llm']
    prompt = f"""
    You are an ATS (Applicant Tracking System) Judge. Score the following tailored resume against the job description.
    Focus on keywords and parseability.
    Return ONLY a JSON object with 'score' (0-100) and 'feedback' (string).

    Tailored Resume:
    {state['optimized_resume']}

    Job Description:
    {state['job_description']}
    """
    response = llm.invoke([HumanMessage(content=prompt)])
    # In a real scenario, we'd use structured output. For now, simple parsing.
    import json
    import re
    # Attempt to find JSON in the response
    match = re.search(r'\{.*\}', response.content, re.DOTALL)
    if match:
        data = json.loads(match.group())
    else:
        # Fallback if LLM fails to provide JSON
        data = {"score": 70, "feedback": "Could not parse score. Defaulting."}

    return {
        "ats_score": data.get('score', 0),
        "feedback": state['feedback'] + [f"ATS: {data.get('feedback', '')}"]
    }

def recruiter_judge_node(state: AgentState):
    llm = state['llm']
    prompt = f"""
    You are a Senior Recruiter. Score the following tailored resume against the job description.
    Focus on metrics, impact, and action verbs.
    Return ONLY a JSON object with 'score' (0-100) and 'feedback' (string).

    Tailored Resume:
    {state['optimized_resume']}

    Job Description:
    {state['job_description']}
    """
    response = llm.invoke([HumanMessage(content=prompt)])
    import json
    import re
    match = re.search(r'\{.*\}', response.content, re.DOTALL)
    if match:
        data = json.loads(match.group())
    else:
        data = {"score": 70, "feedback": "Could not parse score. Defaulting."}

    return {
        "recruiter_score": data.get('score', 0),
        "feedback": state['feedback'] + [f"Recruiter: {data.get('feedback', '')}"]
    }

# --- Graph Logic ---

def should_continue(state: AgentState):
    avg_score = (state['ats_score'] + state['recruiter_score']) / 2
    if state['iteration_count'] >= 3 or avg_score >= 85:
        return END
    else:
        return "writer"

def create_workflow():
    workflow = StateGraph(AgentState)

    workflow.add_node("writer", writer_node)
    workflow.add_node("ats_judge", ats_judge_node)
    workflow.add_node("recruiter_judge", recruiter_judge_node)

    workflow.set_entry_point("writer")

    workflow.add_edge("writer", "ats_judge")
    workflow.add_edge("ats_judge", "recruiter_judge")

    workflow.add_conditional_edges(
        "recruiter_judge",
        should_continue
    )

    return workflow.compile()
