# -*- coding: utf-8 -*-
import sys
import os
import time
import random
import shutil
import json
import threading
import pyperclip
import hashlib
import uuid
import requests # CẦN CÀI: pip install requests
import atexit
from datetime import datetime, timedelta, timezone

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QGroupBox, QFileDialog,
    QMessageBox, QListWidget, QListWidgetItem, QInputDialog, 
    QCheckBox, QPlainTextEdit, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QSplitter, QSpinBox, QRadioButton, QButtonGroup, QLineEdit
)
from PyQt5.QtGui import QFont, QColor
from PyQt5.QtCore import Qt, QSettings, QThreadPool, QRunnable, QObject, pyqtSignal, QThread

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
import subprocess

# --- CẤU HÌNH ---
PROFILE_BASE_DIR = os.path.join(os.getcwd(), "chrome_profiles")
HISTORY_FILE = os.path.join(os.getcwd(), "comment_history.json")
if not os.path.exists(PROFILE_BASE_DIR): os.makedirs(PROFILE_BASE_DIR)

# --- API CONFIG ---
API_URL = "https://script.google.com/macros/s/AKfycbxPJVeR9WZ8AFJEAWv0j6p6DtgeTYoKLjhRBJPOGtkfqleViuZn5UrswRv-ikQTAyvGRw/exec"
TOOL_NAME = "FBAutoToolPro"

# ============================================================================
# LOGIC XÁC THỰC BẢN QUYỀN (AUTH LOGIC)
# ============================================================================
def get_pc_id():
    mac = uuid.getnode()
    return ':'.join(("%012X" % mac)[i:i+2] for i in range(0, 12, 2))

def api_request(action, key, tool_name=None, pc_id=None, retries=3):
    headers = {"Content-Type": "application/json"}
    data = {"action": action, "key": key}
    if tool_name: data["tool"] = tool_name
    if pc_id: data["pc_id"] = pc_id
    
    for attempt in range(retries):
        try:
            if action in ["register", "unregister", "refresh"]:
                response = requests.post(API_URL, headers=headers, data=json.dumps(data), timeout=10)
            else:
                response = requests.get(API_URL, params=data, timeout=10)
            
            res_data = response.json()
            if "error" in res_data and "Khóa không thành công" in res_data["error"]:
                time.sleep(1)
                continue
            return res_data
        except Exception as e:
            if attempt == retries - 1:
                return {"error": f"Lỗi kết nối: {str(e)}"}
    return {"error": "Không thể kết nối Server"}

def unregister_pc_id(key, pc_id):
    if key and pc_id:
        try:
            requests.post(API_URL, json={"action": "unregister", "key": key, "pc_id": pc_id}, timeout=5)
        except: pass

class AuthThread(QThread):
    result_signal = pyqtSignal(bool, str, str) # success, message, expire_date

    def __init__(self, key):
        super().__init__()
        self.key = key.strip()

    def run(self):
        # 1. Check Key Exist
        data = api_request("check", self.key, TOOL_NAME)
        if "error" in data:
            self.result_signal.emit(False, data['error'], "")
            return

        # 2. Check Expire Date
        expire_date_str = data["expire"]
        try:
            expire_date = datetime.strptime(expire_date_str, "%d/%m/%Y")
            now_gmt7 = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=7)))
            if expire_date.date() < now_gmt7.date():
                self.result_signal.emit(False, f"Key đã hết hạn vào {expire_date_str}", "")
                return
        except ValueError:
            self.result_signal.emit(False, "Lỗi định dạng ngày từ Server", "")
            return

        # 3. Register PC
        pc_id = get_pc_id()
        reg_data = api_request("register", self.key, TOOL_NAME, pc_id)
        
        if "error" in reg_data:
            if reg_data["error"] == "PC ID đã được đăng ký":
                # Re-check valid owner (trường hợp key đúng máy nhưng server chưa sync)
                pass 
            else:
                self.result_signal.emit(False, reg_data['error'], "")
                return

        self.result_signal.emit(True, "Xác thực thành công!", expire_date_str)

class RefreshTokenThread(QThread):
    def __init__(self, key, pc_id):
        super().__init__()
        self.key = key
        self.pc_id = pc_id
        self.is_running = True

    def run(self):
        while self.is_running:
            try:
                api_request("refresh", self.key, pc_id=self.pc_id)
            except: pass
            # Sleep 12 hours
            for _ in range(12 * 60 * 60):
                if not self.is_running: break
                time.sleep(1)
    
    def stop(self):
        self.is_running = False


# ============================================================================
# LOGIC QUẢN LÝ LỊCH SỬ BÌNH LUẬN
# ============================================================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return []
    return []

def save_history(content_hash):
    history = load_history()
    if content_hash not in history:
        history.append(content_hash)
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f)

def check_is_duplicate(content):
    if not content: return False
    content_hash = hashlib.md5(content.strip().encode('utf-8')).hexdigest()
    history = load_history()
    return content_hash in history

# ============================================================================
# WORKERS (POST, COMMENT, CLONE)
# ============================================================================
class FacebookPostWorker(QRunnable):
    class Signals(QObject):
        log = pyqtSignal(str)
        update_status = pyqtSignal(int, str)
        finished = pyqtSignal(str, int)

    def __init__(self, job_data, row_index, headless):
        super().__init__()
        self.job = job_data
        self.row_index = row_index
        self.headless = headless
        self.signals = self.Signals()
        self.driver = None
        self.is_running = True

    def stop(self):
        self.is_running = False
        if self.driver:
            try: self.driver.quit()
            except: pass

    def log_msg(self, msg): self.signals.log.emit(msg)

    def paste_content(self, text):
        try:
            pyperclip.copy(text)
            time.sleep(0.5)
            act = ActionChains(self.driver)
            act.key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
            time.sleep(1)
        except: pass

    def scrape_all_groups(self):
        self.log_msg(f"[{self.job['profile_name']}] Đang quét nhóm...")
        self.driver.get("https://www.facebook.com/groups/joins")
        time.sleep(5)
        found_groups = []
        last_height = self.driver.execute_script("return document.body.scrollHeight")
        while self.is_running:
            try:
                xpath = '//div[@role="list"]//div[@role="listitem"]//a[@role="link"]'
                elements = self.driver.find_elements(By.XPATH, xpath)
                for el in elements:
                    try:
                        href = el.get_attribute("href")
                        if href and "groups/" in href:
                            clean = href.split("?")[0].rstrip('/')
                            if clean not in found_groups: found_groups.append(clean)
                    except: continue
            except: pass
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height: break
            last_height = new_height
        return found_groups

    def run(self):
        self.signals.update_status.emit(self.row_index, "Running...")
        self.log_msg(f"[{self.job['profile_name']}] >>> Bắt đầu ĐĂNG BÀI...")
        service = Service(executable_path=os.path.join(os.getcwd(), 'chromedriver.exe'))
        options = webdriver.ChromeOptions()
        options.add_argument(f'--user-data-dir={self.job["profile_path"]}')
        options.add_argument('--disable-notifications')
        if self.headless: options.add_argument('--headless=new')

        try:
            self.driver = webdriver.Chrome(service=service, options=options)
            wait = WebDriverWait(self.driver, 20)
            
            target_links = self.job['links']
            if not target_links:
                target_links = self.scrape_all_groups()

            for idx, group_url in enumerate(target_links):
                if not self.is_running: break
                try:
                    self.log_msg(f"[{self.job['profile_name']}] ({idx+1}/{len(target_links)}) Vào: {group_url}")
                    self.driver.get(group_url)
                    time.sleep(4)
                    
                    btn = None
                    for xp in ["//span[text()='Bạn viết gì đi...']", "//div[contains(@aria-label, 'Tạo bài viết')]"]:
                        try: 
                            btn = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                            break
                        except: continue
                    
                    if btn:
                        btn.click()
                        time.sleep(2)
                        if self.job['content']:
                            self.paste_content(random.choice(self.job['content']))
                            time.sleep(2)
                        
                        if self.job['media']:
                            try:
                                file_input = self.driver.find_element(By.XPATH, "//input[@type='file' and @multiple]")
                                valid_exts = ('.jpg', '.png', '.mp4')
                                files = [os.path.join(self.job['media'], f) for f in os.listdir(self.job['media']) if f.endswith(valid_exts)]
                                if files: file_input.send_keys("\n".join(files))
                                time.sleep(10)
                            except: pass

                        try:
                            post = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[text()='Đăng' or text()='Post']")))
                            post.click()
                            self.log_msg("✅ Đã đăng.")
                            time.sleep(random.randint(5, 10))
                        except: pass
                    else:
                        self.log_msg("⚠️ Không tìm thấy nút đăng.")
                except Exception as e:
                    self.log_msg(f"Lỗi nhóm: {e}")

        except Exception as e:
            self.log_msg(f"Lỗi trình duyệt: {e}")
        finally:
            if self.driver: self.driver.quit()
            self.signals.finished.emit(self.job['profile_name'], self.row_index)

class FacebookCommentWorker(QRunnable):
    class Signals(QObject):
        log = pyqtSignal(str)
        update_status = pyqtSignal(int, str)
        finished = pyqtSignal(str, int)

    def __init__(self, job_data, row_index, headless):
        super().__init__()
        self.job = job_data
        self.row_index = row_index
        self.headless = headless
        self.signals = self.Signals()
        self.driver = None
        self.is_running = True

    def stop(self):
        self.is_running = False
        if self.driver:
            try: self.driver.quit()
            except: pass

    def log_msg(self, msg): self.signals.log.emit(msg)

    def paste_content(self, text):
        try:
            pyperclip.copy(text)
            time.sleep(0.5)
            act = ActionChains(self.driver)
            act.key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
            time.sleep(1)
        except: pass

    def process_feed(self, limit_count):
        wait = WebDriverWait(self.driver, 10)
        
        for i in range(1, limit_count + 1):
            if not self.is_running: break
            self.log_msg(f"--- Đang xử lý bài viết thứ {i} ---")
            xpath_post = f'//div[@aria-posinset="{i}"]'
            try:
                found = False
                for _ in range(5):
                    try:
                        post_elm = self.driver.find_element(By.XPATH, xpath_post)
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_elm)
                        found = True
                        break
                    except:
                        self.driver.execute_script("window.scrollBy(0, 500);")
                        time.sleep(1)
                
                if not found:
                    self.log_msg(f"⚠️ Hết bài viết. Dừng.")
                    break
                    
                time.sleep(2)
                
                if self.job['check_duplicate']:
                    try:
                        xpath_content = f'{xpath_post}//div[@data-ad-rendering-role="story_message"]'
                        content_elm = self.driver.find_element(By.XPATH, xpath_content)
                        content_text = content_elm.text
                        if check_is_duplicate(content_text):
                            self.log_msg(f"🚫 Bài viết {i} đã bình luận trước đó. Bỏ qua.")
                            continue 
                    except: pass

                if self.job['like_first']:
                    try:
                        xpath_like = f'{xpath_post}//div[@data-visualcompletion="ignore-dynamic"]//div[(@aria-label="Thích" or @aria-label="Like") and .//div[@data-ad-rendering-role="like_button"]]'
                        like_btn = self.driver.find_element(By.XPATH, xpath_like)
                        like_btn.click()
                        self.log_msg(f"👍 Đã like bài {i}")
                        time.sleep(1)
                    except: pass

                xpath_comment_btn = f'{xpath_post}//div[@data-visualcompletion="ignore-dynamic"]//div[(@aria-label="Viết bình luận" or @aria-label="Leave a comment") and .//div[@data-ad-rendering-role="comment_button"]]'
                try:
                    cmt_btn = self.driver.find_element(By.XPATH, xpath_comment_btn)
                    cmt_btn.click()
                    time.sleep(3)
                except:
                    self.log_msg(f"❌ Không thấy nút bình luận bài {i}")
                    continue

                xpath_form = '//form[@role="presentation"][.//div[@id="focused-state-composer-submit"]]'
                try:
                    wait.until(EC.presence_of_element_located((By.XPATH, xpath_form)))
                    
                    if self.job['content']:
                        text = random.choice(self.job['content'])
                        self.paste_content(text)
                        time.sleep(1)
                    
                    if self.job['media']:
                        try:
                            file_input = self.driver.find_element(By.XPATH, f"{xpath_form}//input[@type='file']")
                            valid_exts = ('.jpg', '.png', '.mp4')
                            files = [os.path.join(self.job['media'], f) for f in os.listdir(self.job['media']) if f.endswith(valid_exts)]
                            if files: 
                                file_input.send_keys("\n".join(files))
                                time.sleep(10)
                        except: pass
                    
                    xpath_send = '//div[@aria-label="Bình luận" or @aria-label="Comment"]'
                    send_btn = self.driver.find_element(By.XPATH, xpath_send)
                    if send_btn.get_attribute("aria-disabled") == "true":
                        time.sleep(5)
                        
                    send_btn.click()
                    self.log_msg(f"✅ Đã gửi bình luận bài {i}")
                    
                    if self.job['check_duplicate'] and 'content_text' in locals():
                        content_hash = hashlib.md5(content_text.strip().encode('utf-8')).hexdigest()
                        save_history(content_hash)
                    
                    time.sleep(3)
                    
                    try:
                        close_btn = self.driver.find_element(By.XPATH, '//div[@aria-label="Đóng" or @aria-label="Close"]')
                        close_btn.click()
                    except: 
                        ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()

                    time.sleep(random.randint(3, 5))
                    
                except Exception as e:
                    ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()

            except Exception as e:
                self.log_msg(f"Lỗi xử lý bài {i}: {e}")

    def run(self):
        self.signals.update_status.emit(self.row_index, "Running...")
        self.log_msg(f"[{self.job['profile_name']}] >>> Bắt đầu BÌNH LUẬN...")
        service = Service(executable_path=os.path.join(os.getcwd(), 'chromedriver.exe'))
        options = webdriver.ChromeOptions()
        options.add_argument(f'--user-data-dir={self.job["profile_path"]}')
        options.add_argument('--disable-notifications')
        if self.headless: options.add_argument('--headless=new')

        try:
            self.driver = webdriver.Chrome(service=service, options=options)
            limit = self.job['limit']
            
            if self.job['mode'] == 'feed':
                self.log_msg(f"Truy cập Feed Nhóm...")
                self.driver.get("https://www.facebook.com/groups/feed/")
                time.sleep(5)
                self.process_feed(limit)
                
            elif self.job['mode'] == 'groups':
                links = self.job['links']
                for grp_link in links:
                    if not self.is_running: break
                    self.log_msg(f"Truy cập nhóm: {grp_link}")
                    self.driver.get(grp_link)
                    time.sleep(5)
                    self.process_feed(limit)
                    
        except Exception as e:
            self.log_msg(f"Lỗi trình duyệt: {e}")
        finally:
            if self.driver: self.driver.quit()
            self.signals.finished.emit(self.job['profile_name'], self.row_index)

# ============================================================================
# MAIN WINDOW
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FB Auto Tool Pro - By Dev Enzo")
        self.setGeometry(100, 100, 1300, 850)
        self.setStyleSheet("""
            QMainWindow { background-color: #f0f2f5; }
            QGroupBox { font-weight: bold; background: white; border: 1px solid #ccc; border-radius: 5px; margin-top: 10px; padding: 10px; }
            QPushButton { background: #1877f2; color: white; padding: 8px; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background: #166fe5; }
            QTextEdit, QPlainTextEdit, QListWidget, QTableWidget { border: 1px solid #ddd; padding: 5px; }
            QLineEdit { padding: 5px; border: 1px solid #ccc; border-radius: 4px; }
            /* Style cho Box Xác thực */
            QGroupBox#AuthBox { background-color: #f8d7da; border: 1px solid #f5c6cb; }
            QGroupBox#AuthBox[verified="true"] { background-color: #d4edda; border: 1px solid #c3e6cb; }
        """)

        self.settings = QSettings("MyTool", "Config")
        self.threadpool = QThreadPool()
        self.running_workers = {}
        self.fb_content_list = []
        self.pending_jobs = [] 
        
        self.is_authenticated = False # Mặc định chưa xác thực
        self.refresh_thread = None
        self.current_key = ""
        self.pc_id = get_pc_id()

        self.init_ui()
        self.load_settings()
        
        # Đăng ký unregister khi tắt app
        atexit.register(self.cleanup)

    def cleanup(self):
        if self.is_authenticated and self.refresh_thread:
            self.refresh_thread.stop()
            unregister_pc_id(self.current_key, self.pc_id)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Tabs
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # TAB 1: DANH SÁCH CHỜ (Chứa phần Xác thực)
        self.tab_queue = QWidget()
        self.setup_tab_queue()
        self.tabs.addTab(self.tab_queue, "📊 Danh Sách Chờ")

        # TAB 2: ĐĂNG BÀI
        self.tab_post = QWidget()
        self.setup_tab_post()
        self.tabs.addTab(self.tab_post, "📝 Lên Lịch Đăng Bài")

        # TAB 4: BÌNH LUẬN
        self.tab_comment = QWidget()
        self.setup_tab_comment()
        self.tabs.addTab(self.tab_comment, "💬 Tự Động Bình Luận")

        # TAB 3: PROFILE
        self.tab_profile = QWidget()
        self.setup_tab_profile()
        self.tabs.addTab(self.tab_profile, "👤 Quản Lý Profile")

        # LOG
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True); self.log_box.setMaximumHeight(150)
        main_layout.addWidget(QLabel("Nhật ký:"))
        main_layout.addWidget(self.log_box)

    def setup_tab_queue(self):
        layout = QVBoxLayout(self.tab_queue)
        
        # --- PHẦN XÁC THỰC BẢN QUYỀN ---
        self.gb_auth = QGroupBox("Xác thực bản quyền")
        self.gb_auth.setObjectName("AuthBox")
        self.gb_auth.setProperty("verified", False)
        
        auth_layout = QVBoxLayout()
        
        h_key = QHBoxLayout()
        h_key.addWidget(QLabel("Nhập Key:"))
        self.txt_key = QLineEdit()
        self.txt_key.setPlaceholderText("Nhập key bản quyền vào đây...")
        h_key.addWidget(self.txt_key)
        auth_layout.addLayout(h_key)
        
        self.lbl_auth_status = QLabel("Chưa xác thực")
        self.lbl_auth_status.setStyleSheet("color: red; font-weight: bold;")
        auth_layout.addWidget(self.lbl_auth_status)
        
        self.btn_auth = QPushButton("Xác nhận Key")
        self.btn_auth.setStyleSheet("background-color: #28a745; color: white;")
        self.btn_auth.clicked.connect(self.verify_key)
        auth_layout.addWidget(self.btn_auth)
        
        self.gb_auth.setLayout(auth_layout)
        layout.addWidget(self.gb_auth)
        # --------------------------------

        self.table_queue = QTableWidget()
        self.table_queue.setColumnCount(7)
        self.table_queue.setHorizontalHeaderLabels(["ID", "Loại", "Profile", "Chi tiết", "Media", "Trạng thái", ""])
        self.table_queue.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.table_queue)

        h = QHBoxLayout()
        self.chk_headless = QCheckBox("Chạy ẩn (Headless)")
        btn_run = QPushButton("▶️ CHẠY TẤT CẢ DANH SÁCH"); btn_run.clicked.connect(self.run_queue)
        btn_del = QPushButton("❌ Xóa dòng chọn"); btn_del.clicked.connect(self.delete_queue)
        btn_stop = QPushButton("⛔ DỪNG KHẨN CẤP"); btn_stop.setStyleSheet("background:#dc3545"); btn_stop.clicked.connect(self.stop_all)
        h.addWidget(self.chk_headless); h.addStretch(); h.addWidget(btn_del); h.addWidget(btn_run); h.addWidget(btn_stop)
        layout.addLayout(h)

    def verify_key(self):
        key = self.txt_key.text().strip()
        if not key:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập Key!")
            return
        
        self.btn_auth.setEnabled(False)
        self.btn_auth.setText("Đang kiểm tra...")
        self.log("Đang kết nối server kiểm tra key...")
        
        self.auth_worker = AuthThread(key)
        self.auth_worker.result_signal.connect(self.on_auth_result)
        self.auth_worker.start()

    def on_auth_result(self, success, message, expire_date):
        self.btn_auth.setEnabled(True)
        self.btn_auth.setText("Xác nhận Key")
        
        if success:
            self.is_authenticated = True
            self.current_key = self.txt_key.text().strip()
            
            # Update UI
            self.gb_auth.setProperty("verified", True)
            self.gb_auth.style().unpolish(self.gb_auth)
            self.gb_auth.style().polish(self.gb_auth)
            
            self.lbl_auth_status.setText(f"Đã xác thực thành công. Hết hạn: {expire_date}")
            self.lbl_auth_status.setStyleSheet("color: green; font-weight: bold;")
            self.txt_key.setReadOnly(True)
            self.btn_auth.setVisible(False)
            
            # Start Refresh Token Thread
            self.refresh_thread = RefreshTokenThread(self.current_key, self.pc_id)
            self.refresh_thread.start()
            
            QMessageBox.information(self, "Thành công", f"Key hợp lệ!\nNgày hết hạn: {expire_date}\nBạn có thể sử dụng tool.")
        else:
            self.is_authenticated = False
            self.lbl_auth_status.setText(message)
            QMessageBox.critical(self, "Lỗi Xác Thực", message)

    def check_auth(self):
        if not self.is_authenticated:
            QMessageBox.warning(self, "Chưa kích hoạt", "Vui lòng nhập và xác thực Key bản quyền tại Tab 1 trước khi sử dụng!")
            return False
        return True

    def setup_tab_post(self):
        layout = QHBoxLayout(self.tab_post)
        g1 = QGroupBox("Chọn Profile"); v1 = QVBoxLayout(); self.lst_post_prof = QListWidget(); self.lst_post_prof.setSelectionMode(QListWidget.MultiSelection); v1.addWidget(self.lst_post_prof); g1.setLayout(v1)
        
        g2 = QGroupBox("Cấu hình"); v2 = QVBoxLayout()
        v2.addWidget(QLabel("Link Nhóm (Để trống = Đăng tất cả):")); self.txt_post_links = QPlainTextEdit(); v2.addWidget(self.txt_post_links)
        v2.addWidget(QLabel("Media:")); h_m = QHBoxLayout(); self.txt_post_media = QTextEdit(); self.txt_post_media.setMaximumHeight(35); btn_m = QPushButton("..."); btn_m.clicked.connect(lambda: self.browse_media(self.txt_post_media)); h_m.addWidget(self.txt_post_media); h_m.addWidget(btn_m); v2.addLayout(h_m); g2.setLayout(v2)

        g3 = QGroupBox("Nội dung"); v3 = QVBoxLayout()
        self.txt_post_content = QTextEdit(); v3.addWidget(self.txt_post_content)
        h_c = QHBoxLayout(); b_add = QPushButton("Thêm"); b_add.clicked.connect(self.add_content_post); h_c.addWidget(b_add); b_clr = QPushButton("Xóa"); b_clr.clicked.connect(self.clear_content_post); h_c.addWidget(b_clr); v3.addLayout(h_c)
        self.lst_post_content_view = QListWidget(); v3.addWidget(self.lst_post_content_view)
        b_q = QPushButton("THÊM VÀO QUEUE"); b_q.clicked.connect(self.add_post_queue); v3.addWidget(b_q); g3.setLayout(v3)
        layout.addWidget(g1, 1); layout.addWidget(g2, 2); layout.addWidget(g3, 2)

    def setup_tab_comment(self):
        layout = QHBoxLayout(self.tab_comment)
        g1 = QGroupBox("1. Chọn Profile"); v1 = QVBoxLayout(); self.lst_cmt_prof = QListWidget(); self.lst_cmt_prof.setSelectionMode(QListWidget.MultiSelection); v1.addWidget(self.lst_cmt_prof); btn_ref = QPushButton("Làm mới"); btn_ref.clicked.connect(self.refresh_profiles); v1.addWidget(btn_ref); g1.setLayout(v1)

        g2 = QGroupBox("2. Chế độ & Cài đặt"); v2 = QVBoxLayout()
        self.rb_cmt_feed = QRadioButton("Feed Nhóm"); self.rb_cmt_feed.setChecked(True)
        self.rb_cmt_group = QRadioButton("Từng Nhóm"); bg = QButtonGroup(self); bg.addButton(self.rb_cmt_feed); bg.addButton(self.rb_cmt_group)
        v2.addWidget(self.rb_cmt_feed); v2.addWidget(self.rb_cmt_group)
        v2.addWidget(QLabel("Link Nhóm (Nếu chọn Từng Nhóm):")); self.txt_cmt_links = QPlainTextEdit(); v2.addWidget(self.txt_cmt_links)
        h_spin = QHBoxLayout(); h_spin.addWidget(QLabel("Số lượng comment:")); self.spin_cmt_limit = QSpinBox(); self.spin_cmt_limit.setRange(1, 100); self.spin_cmt_limit.setValue(10); h_spin.addWidget(self.spin_cmt_limit); v2.addLayout(h_spin)
        self.chk_cmt_like = QCheckBox("Like trước khi Comment"); self.chk_cmt_uniq = QCheckBox("Tránh trùng lặp"); self.chk_cmt_uniq.setChecked(True); v2.addWidget(self.chk_cmt_like); v2.addWidget(self.chk_cmt_uniq)
        v2.addWidget(QLabel("Media:")); h_m = QHBoxLayout(); self.txt_cmt_media = QTextEdit(); self.txt_cmt_media.setMaximumHeight(35); btn_m = QPushButton("..."); btn_m.clicked.connect(lambda: self.browse_media(self.txt_cmt_media)); h_m.addWidget(self.txt_cmt_media); h_m.addWidget(btn_m); v2.addLayout(h_m); g2.setLayout(v2)

        g3 = QGroupBox("3. Nội dung"); v3 = QVBoxLayout(); self.txt_cmt_content = QTextEdit(); v3.addWidget(self.txt_cmt_content)
        h_c = QHBoxLayout(); b_add = QPushButton("Thêm"); b_add.clicked.connect(self.add_content_cmt); b_clr = QPushButton("Xóa"); b_clr.clicked.connect(self.clear_content_cmt); h_c.addWidget(b_add); h_c.addWidget(b_clr); v3.addLayout(h_c); self.lst_cmt_content_view = QListWidget(); v3.addWidget(self.lst_cmt_content_view)
        b_addq = QPushButton("THÊM VÀO QUEUE"); b_addq.setStyleSheet("background:#28a745"); b_addq.clicked.connect(self.add_cmt_queue); v3.addWidget(b_addq); g3.setLayout(v3)
        layout.addWidget(g1, 1); layout.addWidget(g2, 2); layout.addWidget(g3, 2)

    def setup_tab_profile(self):
        layout = QVBoxLayout(self.tab_profile)
        self.mgr_lst_prof = QListWidget(); layout.addWidget(self.mgr_lst_prof)
        h = QHBoxLayout(); b1 = QPushButton("Tạo"); b1.clicked.connect(self.create_profile); b2 = QPushButton("Mở"); b2.clicked.connect(self.open_profile); b3 = QPushButton("Xóa"); b3.clicked.connect(self.delete_profile); h.addWidget(b1); h.addWidget(b2); h.addWidget(b3); layout.addLayout(h)

    def add_post_queue(self):
        if not self.check_auth(): return
        profs = self.lst_post_prof.selectedItems()
        if not profs: return QMessageBox.warning(self, "Lỗi", "Chọn Profile!")
        links = [x.strip() for x in self.txt_post_links.toPlainText().split('\n') if x.strip()]
        media = self.txt_post_media.toPlainText().strip()
        for p in profs:
            job = {'type': 'POST', 'id': len(self.pending_jobs)+1, 'profile_name': p.text(), 'profile_path': p.data(Qt.UserRole), 'links': links, 'content': self.fb_content_list[:], 'media': media, 'status': 'Chờ chạy'}
            self.add_job_to_table(job)

    def add_cmt_queue(self):
        if not self.check_auth(): return
        profs = self.lst_cmt_prof.selectedItems()
        if not profs: return QMessageBox.warning(self, "Lỗi", "Chọn Profile!")
        mode = 'feed' if self.rb_cmt_feed.isChecked() else 'groups'
        links = [x.strip() for x in self.txt_cmt_links.toPlainText().split('\n') if x.strip()] if mode == 'groups' else []
        media = self.txt_cmt_media.toPlainText().strip()
        contents = [self.lst_cmt_content_view.item(i).text() for i in range(self.lst_cmt_content_view.count())]
        if not contents and not media: return QMessageBox.warning(self, "Lỗi", "Cần nội dung!")
        for p in profs:
            job = {'type': 'COMMENT', 'id': len(self.pending_jobs)+1, 'profile_name': p.text(), 'profile_path': p.data(Qt.UserRole), 'mode': mode, 'links': links, 'limit': self.spin_cmt_limit.value(), 'like_first': self.chk_cmt_like.isChecked(), 'check_duplicate': self.chk_cmt_uniq.isChecked(), 'content': contents, 'media': media, 'status': 'Chờ chạy'}
            self.add_job_to_table(job)

    def add_job_to_table(self, job):
        self.pending_jobs.append(job)
        row = self.table_queue.rowCount()
        self.table_queue.insertRow(row)
        self.table_queue.setItem(row, 0, QTableWidgetItem(str(job['id'])))
        self.table_queue.setItem(row, 1, QTableWidgetItem(job['type']))
        self.table_queue.setItem(row, 2, QTableWidgetItem(job['profile_name']))
        detail = f"Feed ({job['limit']})" if job['type']=='COMMENT' and job['mode']=='feed' else f"{len(job['links'])} nhóm"
        self.table_queue.setItem(row, 3, QTableWidgetItem(detail))
        self.table_queue.setItem(row, 4, QTableWidgetItem("Có" if job['media'] else "No"))
        self.table_queue.setItem(row, 5, QTableWidgetItem("Chờ chạy"))
        self.tabs.setCurrentIndex(0)

    def run_queue(self):
        if not self.check_auth(): return
        headless = self.chk_headless.isChecked()
        for i in range(self.table_queue.rowCount()):
            item = self.table_queue.item(i, 5)
            if item.text() == "Chờ chạy":
                job = self.pending_jobs[i]
                item.setText("Running..."); item.setForeground(QColor("blue"))
                w = FacebookPostWorker(job, i, headless) if job['type'] == 'POST' else FacebookCommentWorker(job, i, headless)
                w.signals.log.connect(self.log); w.signals.update_status.connect(self.update_status); w.signals.finished.connect(self.job_done)
                self.running_workers[f"job_{i}"] = w; self.threadpool.start(w); time.sleep(2)

    def update_status(self, row, msg): self.table_queue.item(row, 5).setText(msg)
    def job_done(self, name, row): item = self.table_queue.item(row, 5); item.setText("Hoàn thành"); item.setForeground(QColor("green")); self.log(f"[{name}] Xong.")
    def delete_queue(self): 
        for r in sorted(set(x.row() for x in self.table_queue.selectedIndexes()), reverse=True): self.table_queue.removeRow(r); del self.pending_jobs[r]

    def refresh_profiles(self):
        self.lst_post_prof.clear(); self.lst_cmt_prof.clear(); self.mgr_lst_prof.clear()
        if os.path.exists(PROFILE_BASE_DIR):
            for n in os.listdir(PROFILE_BASE_DIR):
                p = os.path.join(PROFILE_BASE_DIR, n)
                if os.path.isdir(p):
                    for l in [self.lst_post_prof, self.lst_cmt_prof, self.mgr_lst_prof]: item = QListWidgetItem(n); item.setData(Qt.UserRole, p); l.addItem(item)
    
    def create_profile(self):
        if not self.check_auth(): return
        n, ok = QInputDialog.getText(self, "Tạo", "Tên:"); 
        if ok and n: os.makedirs(os.path.join(PROFILE_BASE_DIR, "".join(x for x in n if x.isalnum() or x=='_'))); self.refresh_profiles()
    
    def delete_profile(self):
        if not self.check_auth(): return
        for i in self.mgr_lst_prof.selectedItems(): shutil.rmtree(i.data(Qt.UserRole))
        self.refresh_profiles()
    
    def open_profile(self):
        if not self.check_auth(): return
        i = self.mgr_lst_prof.currentItem()
        if i: subprocess.Popen([r"C:\Program Files\Google\Chrome\Application\chrome.exe", f"--user-data-dir={i.data(Qt.UserRole)}"])

    def browse_media(self, txt_widget):
        if not self.check_auth(): return
        d = QFileDialog.getExistingDirectory(self, "Chọn folder"); 
        if d: txt_widget.setText(d)

    def add_content_post(self): t = self.txt_post_content.toPlainText().strip(); (self.fb_content_list.append(t), self.lst_post_content_view.addItem(t[:30]+"..."), self.txt_post_content.clear()) if t else None
    def clear_content_post(self): self.fb_content_list = []; self.lst_post_content_view.clear()
    def add_content_cmt(self): t = self.txt_cmt_content.toPlainText().strip(); (self.lst_cmt_content_view.addItem(t), self.txt_cmt_content.clear()) if t else None
    def clear_content_cmt(self): self.lst_cmt_content_view.clear()
    def stop_all(self): [w.stop() for w in self.running_workers.values()]
    def log(self, msg): self.log_box.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"); self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())
    def save_settings(self): pass
    def load_settings(self): self.refresh_profiles()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())