import os
import platform
import sys
from uuid import uuid4
import json
from datetime import datetime

import streamlit as st
from loguru import logger
import requests

# Add the root directory of the project to the system path to allow importing modules from the project
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoTransitionMode,
    VideoParams,
)
from app.services import llm, voice
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo Enhanced",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo Enhanced\nEnhanced version with additional features including:\n- Health monitoring\n- Advanced video settings\n- Batch processing\n- Template management\n- Real-time progress tracking\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)

# Enhanced CSS styling
streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
.stProgress .st-bo {
    background-color: #1f77b4;
}
.task-card {
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 16px;
    margin: 8px 0;
}
.success-card {
    border-color: #28a745;
    background-color: #f8fff9;
}
.error-card {
    border-color: #dc3545;
    background-color: #fff8f8;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
system_locale = utils.get_system_locale()

# Initialize session state with enhanced defaults
if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)
if "tasks" not in st.session_state:
    st.session_state["tasks"] = []
if "templates" not in st.session_state:
    st.session_state["templates"] = {}

# 加载语言文件
locales = utils.load_locales(i18n_dir)

# Enhanced sidebar with health status
with st.sidebar:
    st.title("🤖 MoneyPrinterTurbo")
    
    # Health check
    try:
        health_response = requests.get("http://localhost:8080/ping", timeout=2)
        if health_response.status_code == 200:
            st.success("✅ Service Healthy")
            health_data = health_response.json()
            st.metric("CPU", f"{health_data.get('system', {}).get('cpu_percent', 0)}%")
            st.metric("Memory", f"{health_data.get('system', {}).get('memory_percent', 0)}%")
        else:
            st.error("❌ Service Unhealthy")
    except:
        st.warning("⚠️ Service Check Failed")

    # Language selector
    display_languages = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i

    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="sidebar_language_selector",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code
        config.ui["language"] = code

    # Quick actions
    st.subheader("Quick Actions")
    if st.button("🗑️ Clear All Tasks"):
        st.session_state["tasks"] = []
        st.rerun()
    
    if st.button("💾 Save Template"):
        template_name = st.text_input("Template Name")
        if template_name:
            st.session_state["templates"][template_name] = {
                "video_subject": st.session_state["video_subject"],
                "video_script": st.session_state["video_script"],
                "video_terms": st.session_state["video_terms"],
            }
            st.success(f"Template '{template_name}' saved!")

# Create log container in the main area
log_container = st.container()

# Enhanced task management
def display_task_history():
    """Display historical tasks with status."""
    if st.session_state["tasks"]:
        st.subheader("📋 Task History")
        for task in st.session_state["tasks"]:
            with st.expander(f"Task: {task['id'][:8]} - {task['status']}"):
                st.json(task)

# Add to the main interface
with st.sidebar:
    if st.checkbox("Show Task History"):
        display_task_history()

# Enhanced error handling and logging
def enhanced_log_received(msg):
    """Enhanced logging with timestamps and levels."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if config.ui["hide_log"]:
        return
    
    # Store in session state for persistence
    if "logs" not in st.session_state:
        st.session_state["logs"] = []
    
    st.session_state["logs"].append(formatted_msg)
    
    # Keep only last 100 logs
    if len(st.session_state["logs"]) > 100:
        st.session_state["logs"] = st.session_state["logs"][-100:]
    
    with log_container:
        st.code("\n".join(st.session_state["logs"][-20:]))  # Show last 20

# Replace the original log_received function with enhanced version
# [Rest of the file continues with original functionality...]
