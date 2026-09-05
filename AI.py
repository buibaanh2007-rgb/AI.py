from datetime import datetime
import os
import re
import time
import unicodedata

from flask import Flask, jsonify, make_response, request, send_file
from gtts import gTTS
import requests
import speech_recognition as sr

app = Flask(__name__)
recognizer = sr.Recognizer()


# --- CẤU HÌNH KẾT NỐI SV2 ---
# Sếp nhớ thay đổi IP và cổng của sv2 cho chính xác nhé
SV2_URL = "http://192.168.1.10:9090/api/sync"

# Biến toàn cục quản lý trạng thái thức/ngủ và timeout 60 giây
is_awake = False
last_active_time = 0
SLEEP_TIMEOUT = 60

# Các biến toàn cục quản lý trạng thái đặt báo thức thông minh
waiting_for_alarm = False
alarm_hour = None
alarm_minute = None
alarm_period = None 
alarm_is_active = False 

# Biến lưu trữ dữ liệu nhận từ sv2 (nếu cần dùng chung)
sv2_shared_data = {}

print("[Server sv1] Đã sẵn sàng chạy kèm cơ chế truyền/nhận dữ liệu với sv2!")


def remove_accents(input_str):
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).lower()


# --- HÀM TRUYỀN DỮ LIỆU ĐI SV2 ---
def send_to_sv2(payload):
    """Hàm gửi dữ liệu sang sv2 (chạy ngầm hoặc gọi trực tiếp)"""
    try:
        response = requests.post(SV2_URL, json=payload, timeout=2)
        if response.status_code == 200:
            print(f"[sv1 -> sv2] Đã gửi data thành công: {payload}")
        else:
            print(f"[sv1 -> sv2] Phản hồi lỗi từ sv2: {response.status_code}")
    except Exception as e:
        print(f"[sv1 -> sv2] Không thể kết nối tới sv2: {e}")


@app.route("/")
def home():
    return "AI Speaker Server SV1 Running!"


# --- ENDPOINT NHẬN DỮ LIỆU TỪ SV2 ---
@app.route("/api/receive-from-sv2", methods=["POST"])
def receive_from_sv2():
    global sv2_shared_data
    if request.is_json:
        data = request.get_json()
        sv2_shared_data = data
        print(f"[sv2 -> sv1] Đã nhận dữ liệu từ sv2: {data}")
        return jsonify({"status": "success", "message": "SV1 received data from SV2"}), 200
    return jsonify({"status": "error", "message": "Invalid JSON"}), 400


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time, waiting_for_alarm, alarm_hour, alarm_minute, alarm_period, alarm_is_active

    current_bot_mode = "DEFAULT"
    
    res_alarm_hour = "NONE"
    res_alarm_minute = "NONE"
    res_alarm_state = "ON" if alarm_is_active else "OFF"  

    # 1. Xử lý sự kiện hệ thống (boot, connected từ ESP32)
    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")
        if event_type == "boot" or event_type == "connected":
            is_awake = False
            waiting_for_alarm = False
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Kết nối server thành công"
            print(f"[Server] Sự kiện hệ thống - Phản hồi: {reply_text}")

            mp3_path = "response.mp3"
            raw_pcm_reply = "response.pcm"
            tts = gTTS(text=reply_text, lang="vi")
            tts.save(mp3_path)
            os.system(
                f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
            )

            if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
                resp = make_response(
                    send_file(raw_pcm_reply, mimetype="application/octet-stream")
                )
                resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
                resp.headers["Bot-Mode"] = "SET_MODE_0"
                resp.headers["Alarm-State"] = "ON" if alarm_is_active else "OFF"
                resp.headers["Alarm-Hour"] = str(alarm_hour) if alarm_hour is not None else "NONE"
                resp.headers["Alarm-Minute"] = str(alarm_minute) if alarm_minute is not None else "NONE"
                return resp
        return "", 204

    # 2. Kiểm tra timeout 60 giây
    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False
        waiting_for_alarm = False
        alarm_hour = None
        alarm_minute = None
        alarm_period = None
        print("[Server] Đã quá 60 giây tự động chuyển về chế độ NGỦ.")

    # 3. Nhận audio thô từ ESP32
    audio_data = request.data
    if len(audio_data) < 500:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        resp.headers["Bot-Mode"] = "DEFAULT"
        resp.headers["Alarm-State"] = "ON" if alarm_is_active else "OFF"
        return resp

    raw_pcm_path = "input_temp.pcm"
    wav_path = "input_temp.wav"

    with open(raw_pcm_path, "wb") as f:
        f.write(audio_data)

    os.system(
        f"ffmpeg -y -f s16le -ar 16000 -ac 1 -i {raw_pcm_path} {wav_path} > /dev/null 2>&1"
    )

    spoken_text = ""
    try:
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            spoken_text = recognizer.recognize_google(
                audio, language="vi-VN"
            ).lower()
            print(f"[Server] Nghe được: '{spoken_text}'")
    except sr.UnknownValueError:
        print("[Server] Không nghe rõ nội dung hoặc khoảng lặng.")
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        resp.headers["Bot-Mode"] = "SET_MODE_1" if waiting_for_alarm else "DEFAULT"
        resp.headers["Alarm-State"] = "ON" if alarm_is_active else "OFF"
        return resp
    except sr.RequestError as e:
        print(f"[Server] Lỗi kết nối Google STT: {e}")
        return "", 500

    # 4. Phân rã logic theo trạng thái THỨC hay NGỦ
    if not is_awake:
        wake_words = ["xin chào", "chào", "ngáo", "xin", "dậy đi"]
        if any(word in spoken_text for word in wake_words):
            is_awake = True
            reply_text = "Chào sếp, sếp cần giúp gì ạ?"
            current_bot_mode = "SET_MODE_1" 
            print("[Server] Trạng thái: ĐÃ THỨC.")
        else:
            resp = make_response("", 204)
            resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
            resp.headers["Bot-Mode"] = "DEFAULT"
            resp.headers["Alarm-State"] = "ON" if alarm_is_active else "OFF"
            return resp
    else:
        text_clean = remove_accents(spoken_text)

        # ƯU TIÊN 1: Đang trong tiến trình đặt báo thức
        if waiting_for_alarm:
            if any(k in spoken_text for k in ["hủy báo thức", "xóa báo thức", "bỏ báo thức", "hủy lịch"]) or any(k in text_clean for k in ["hủy", "thôi", "dừng", "khong dat nua"]):
                waiting_for_alarm = False
                alarm_hour = None
                alarm_minute = None
                alarm_period = None
                alarm_is_active = False
                reply_text = "Đã hủy cài đặt báo thức."
                current_bot_mode = "SET_MODE_1" 
                res_alarm_state = "OFF"
            else:
                match_full = re.search(r'(\d+)\s*(?:giờ|h|:)\s*(\d+)?', spoken_text)
                if match_full:
                    alarm_hour = int(match_full.group(1))
                    if match_full.group(2):
                        alarm_minute = int(match_full.group(2))
               
                words = spoken_text.split()
                for i, w in enumerate(words):
                    if w.isdigit():
                        val = int(w)
                        clean_next_word = remove_accents(words[i+1]) if i + 1 < len(words) else ""
                        if "phut" in clean_next_word:
                            alarm_minute = val
                        elif any(h in clean_next_word for h in ["gio", "h"]):
                            alarm_hour = val
                        elif alarm_hour is None:
                            alarm_hour = val
                        elif alarm_minute is None and alarm_hour is not None:
                            alarm_minute = val

                if any(s in text_clean for s in ["sáng", "am"]):
                    alarm_period = "sáng"
                elif any(b in text_clean for b in ["chiều", "toi", "trua", "pm", "chieu"]):
                    alarm_period = "chiều"

                if alarm_hour is None:
                    reply_text = "Sếp muốn đặt lúc mấy giờ ạ?"
                    current_bot_mode = "SET_MODE_1" 
                elif alarm_minute is None:
                    reply_text = "Sếp muốn đặt phút thứ mấy ạ?"
                    current_bot_mode = "SET_MODE_1" 
                else:
                    if not (0 <= alarm_hour <= 23 and 0 <= alarm_minute <= 59):
                        reply_text = "Giờ hoặc phút không hợp lệ rồi, sếp đọc lại giúp em nhé."
                        current_bot_mode = "SET_MODE_1"
                        alarm_hour = None
                        alarm_minute = None
                        alarm_period = None
                    else:
                        if alarm_period is None:
                            alarm_period = "sáng" if alarm_hour < 12 else "chiều"

                        final_time_str = f"{alarm_hour} giờ {alarm_minute} phút {alarm_period}"
                        reply_text = f"Đã rõ, đặt báo thức lúc {final_time_str}"
                        current_bot_mode = "SET_MODE_1" 
                        
                        alarm_is_active = True
                        res_alarm_state = "ON"
                        res_alarm_hour = str(alarm_hour)
                        res_alarm_minute = str(alarm_minute)
                        waiting_for_alarm = False

                        # Bắn dữ liệu báo thức mới sang sv2
                        send_to_sv2({
                            "event": "alarm_set",
                            "hour": alarm_hour,
                            "minute": alarm_minute,
                            "state": "ON"
                        })

        # ƯU TIÊN 2: Hủy / tắt khẩn cấp trực tiếp
        elif any(k in spoken_text for k in ["hủy báo thức", "xóa báo thức", "bỏ báo thức", "hủy lịch"]) or any(k in text_clean for k in ["huy", "xoa bao thuc", "bo bao thuc"]):
            waiting_for_alarm = False
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            alarm_is_active = False
            reply_text = "Đã xóa báo thức đã đặt ạ."
            current_bot_mode = "SET_MODE_1" 
            res_alarm_state = "OFF"
            
            send_to_sv2({"event": "alarm_cancelled", "state": "OFF"})

        elif any(kw in spoken_text for kw in ["tắt báo thức", "dừng báo thức", "tắt chuông", "dừng chuông"]):
            alarm_is_active = False
            reply_text = "Đã tắt báo thức ạ."
            current_bot_mode = "ALARM_STOP" 
            res_alarm_state = "OFF"
            
            send_to_sv2({"event": "alarm_stopped", "state": "OFF"})

        # ƯU TIÊN 3: Kiểm tra báo thức
        elif any(k in spoken_text for k in ["kiểm tra báo thức", "xem báo thức", "báo thức mấy giờ"]):
            if not alarm_is_active or alarm_hour is None or alarm_minute is None:
                reply_text = "Báo thức đang tắt."
            else:
                period_str = alarm_period if alarm_period else ("sáng" if alarm_hour < 12 else "chiều")
                reply_text = f"Báo thức đang bật lúc {alarm_hour} giờ {alarm_minute} phút {period_str}."
            current_bot_mode = "SET_MODE_1"

        # ƯU TIÊN 4: Đặt báo thức mới
        elif "đặt báo thức" in spoken_text or "báo thức" in spoken_text:
            waiting_for_alarm = True
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Sếp muốn đặt thế nào?"
            current_bot_mode = "SET_MODE_1" 

        # ƯU TIÊN 5: Nhiệt độ phòng / Cảm biến (Đã bổ sung tự động lấy và đẩy `room_temp`, `room_hum` sang sv2)
        elif "nhiệt độ phòng" in spoken_text or "nhiệt" in spoken_text:
            room_temp = request.headers.get("X-Room-Temp", "25")
            room_hum = request.headers.get("X-Room-Hum", "50")
            
            try:
                room_temp = str(round(float(room_temp), 1))
                room_hum = str(round(float(room_hum), 1))
            except:
                pass

            reply_text = f"Nhiệt độ {room_temp} độ C và độ ẩm {room_hum} phần trăm"
            current_bot_mode = "SET_MODE_3" 

            # Truyền dữ liệu cảm biến sang sv2 (cập nhật đồng thời khóa room_temp và room_hum để sv2 nhận diện trực tiếp)
            send_to_sv2({
                "event": "sensor_update",
                "room_temp": room_temp,
                "room_hum": room_hum,
                "temp": room_temp,
                "hum": room_hum
            })

        # ƯU TIÊN 6: Hỏi giờ hiện tại
        elif "mấy giờ rồi" in spoken_text or "bây giờ là mấy giờ" in spoken_text:
            now = datetime.now()
            reply_text = f"Bây giờ là {now.strftime('%H')} giờ {now.strftime('%M')} phút"
            current_bot_mode = "SET_MODE_2" 

        # ƯU TIÊN 7: Thời tiết
        elif "thời tiết" in spoken_text:
            current_bot_mode = "SET_MODE_3" 
            try:
                location = "HaNam"
                parts = spoken_text.split("thời tiết")
                if len(parts) > 1 and parts[1].strip() != "":
                    raw_loc = parts[1].strip().replace("ở", "").replace("tại", "").strip()
                    if raw_loc != "":
                        location = remove_accents(raw_loc).replace(" ", "")

                response = requests.get(f"https://wttr.in/{location}?format=j1", timeout=3)
                if response.status_code == 200:
                    data = response.json()
                    temp = data["current_condition"][0]["temp_C"]
                    humidity = data["current_condition"][0]["humidity"]
                    reply_text = f"Thời tiết {location}. nhiệt độ {temp} độ C. độ ẩm {humidity} percent"
                else:
                    reply_text = "Không tìm thấy thời tiết khu vực này"
            except Exception as e:
                reply_text = "Lỗi kết nối thời tiết"

        # ƯU TIÊN 8: Tính toán
        elif any(op in spoken_text for op in ["cộng", "trừ", "nhân", "chia", "x", "+", "-", "*", "/"]):
            try:
                cleaned_text = (spoken_text
                                .replace("cộng", "+")
                                .replace("trừ", "-")
                                .replace("nhân", "*")
                                .replace(" x ", "*")
                                .replace(" x", "*")
                                .replace("x ", "*")
                                .replace("chia", "/")
                )
                expr = "".join([c for c in cleaned_text if c in "0123456789+-*/. "]).strip()
                if expr:
                    result = eval(expr)
                    if isinstance(result, float) and not result.is_integer():
                        result = round(result, 2)
                    reply_text = f"Kết quả bằng {result} ạ"
                else:
                    reply_text = "sếp đọc lại giúp em."
            except Exception:
                reply_text = "Em không thực hiện được phép tính này."

        # ƯU TIÊN 9: Mode 5
        elif "bật cài đặt" in spoken_text or "hey google" in spoken_text or "mode 5" in spoken_text:
            reply_text = "Đã chuyển sang chế độ nháy nhạc."
            current_bot_mode = "SET_MODE_5"
            is_awake = False  
            
            send_to_sv2({"event": "mode_change", "mode": "SET_MODE_5"})

        # ƯU TIÊN 10: Đi ngủ
        elif "đi ngủ đi" in spoken_text or "ngủ đi" in spoken_text:
            is_awake = False
            waiting_for_alarm = False
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Vâng ạ"
            current_bot_mode = "SET_MODE_0" 

        else:
            reply_text = "Sếp nói lại đi."
            current_bot_mode = "SET_MODE_1" 

    # 5. Tạo file âm thanh phản hồi
    mp3_path = "response.mp3"
    raw_pcm_reply = "response.pcm"

    tts = gTTS(text=reply_text, lang="vi")
    tts.save(mp3_path)
    os.system(f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1")

    if is_awake:
        last_active_time = time.time()

    if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
        resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        resp.headers["Bot-Mode"] = current_bot_mode 
        resp.headers["Alarm-State"] = res_alarm_state
        resp.headers["Alarm-Hour"] = str(alarm_hour) if alarm_hour is not None else "NONE"
        resp.headers["Alarm-Minute"] = str(alarm_minute) if alarm_minute is not None else "NONE"
        return resp
    else:
        return "", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
