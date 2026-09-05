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

# Biến toàn cục quản lý trạng thái Mode 5 (Nháy theo nhạc)
mode_5_active = False

# Biến toàn cục lưu trạng thái cảm biến mới nhất từ S3 để phục vụ Web Dashboard & Giọng nói
current_room_temp = "25.0"
current_room_hum = "50.0"

print("[Server] Đã sẵn sàng chạy theo cơ chế thu âm 2 giây tối ưu!")


def remove_accents(input_str):
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).lower()


@app.route("/")
def home():
    return "AI Speaker Server Running!"


# --- CÁC API PHỤC VỤ WEB DASHBOARD (CỔNG 9090) ---

@app.route("/api/status", methods=["GET"])
def api_status():
    global current_room_temp, current_room_hum, alarm_is_active, alarm_hour, alarm_minute, mode_5_active
    return jsonify({
        "temp": current_room_temp,
        "hum": current_room_hum,
        "alarm_is_active": alarm_is_active,
        "alarm_hour": alarm_hour if alarm_hour is not None else 6,
        "alarm_minute": alarm_minute if alarm_minute is not None else 0,
        "mode_5_active": mode_5_active
    })


@app.route("/api/set-alarm", methods=["POST"])
def api_set_alarm():
    global alarm_is_active, alarm_hour, alarm_minute
    data = request.get_json()
    if data:
        alarm_hour = data.get("hour")
        alarm_minute = data.get("minute")
        alarm_is_active = True
        print(f"[Web API] Đã đặt báo thức qua web: {alarm_hour}:{alarm_minute}")
    return jsonify({"status": "success"})


@app.route("/api/stop-alarm", methods=["POST"])
def api_stop_alarm():
    global alarm_is_active
    alarm_is_active = False
    print("[Web API] Đã tắt báo thức qua web")
    return jsonify({"status": "success"})


@app.route("/api/toggle-mode5", methods=["POST"])
def api_toggle_mode5():
    global mode_5_active
    mode_5_active = not mode_5_active
    print(f"[Web API] Chuyển đổi Mode 5: {mode_5_active}")
    return jsonify({"status": "success", "mode_5_active": mode_5_active})


# API Endpoint nhận dữ liệu cảm biến định kỳ từ ESP32-S3 & trả kèm trạng thái báo thức xuống phần cứng
@app.route("/api/update-sensor", methods=["POST"])
def update_sensor():
    global current_room_temp, current_room_hum, alarm_is_active, alarm_hour, alarm_minute
    if request.is_json:
        data = request.get_json()
        current_room_temp = str(data.get("temp", "25.0"))
        current_room_hum = str(data.get("hum", "50.0"))
        
        # Trả về kèm trạng thái báo thức để bên phần cứng (S3) đồng bộ ngay lập tức
        return jsonify({
            "status": "success",
            "alarm_is_active": alarm_is_active,
            "alarm_hour": alarm_hour if alarm_hour is not None else 0,
            "alarm_minute": alarm_minute if alarm_minute is not None else 0
        }), 200
        
    return jsonify({"status": "error"}), 400


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time, waiting_for_alarm, alarm_hour, alarm_minute, alarm_period, alarm_is_active, mode_5_active
    global current_room_temp, current_room_hum

    current_bot_mode = "DEFAULT"
    
    res_alarm_state = "ON" if alarm_is_active else "OFF"  

    # 1. Xử lý sự kiện hệ thống (boot, connected từ ESP32)
    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")
        if event_type == "boot" or event_type == "connected":
            is_awake = False
            waiting_for_alarm = False
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

    # 2. Kiểm tra timeout 60 giây kể từ lần tương tác trước
    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False
        waiting_for_alarm = False
        print("[Server] Đã quá 60 giây tự động chuyển về chế độ NGỦ.")

    # 3. Nhận audio thô từ ESP32
    audio_data = request.data
    if len(audio_data) < 500:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        resp.headers["Bot-Mode"] = "SET_MODE_1" if waiting_for_alarm else "DEFAULT"
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

        if waiting_for_alarm:
            if any(k in spoken_text for k in ["hủy báo thức", "xóa báo thức"]) or any(k in text_clean for k in ["hủy", "thôi"]):
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
                elif any(b in text_clean for b in ["chiều", "toi", "trua", "pm"]):
                    alarm_period = "chiều"

                if alarm_hour is None:
                    reply_text = "Sếp muốn đặt lúc mấy giờ ạ?"
                    current_bot_mode = "SET_MODE_1" 
                elif alarm_minute is None:
                    reply_text = "Sếp muốn đặt phút thứ mấy ạ?"
                    current_bot_mode = "SET_MODE_1" 
                else:
                    if not (0 <= alarm_hour <= 23 and 0 <= alarm_minute <= 59):
                        reply_text = "Giờ hoặc phút không hợp lệ rồi."
                        current_bot_mode = "SET_MODE_1"
                        alarm_hour = None
                        alarm_minute = None
                    else:
                        if alarm_period is None:
                            alarm_period = "sáng" if alarm_hour < 12 else "chiều"
                        reply_text = f"Đã rõ, đặt báo thức lúc {alarm_hour} giờ {alarm_minute} phút"
                        current_bot_mode = "SET_MODE_1" 
                        alarm_is_active = True
                        res_alarm_state = "ON"
                        waiting_for_alarm = False

        elif "đặt báo thức" in spoken_text or "báo thức" in spoken_text:
            waiting_for_alarm = True
            alarm_hour = None
            alarm_minute = None
            reply_text = "Sếp muốn đặt thế nào?"
            current_bot_mode = "SET_MODE_1" 

        elif "nhiệt độ" in spoken_text:
            reply_text = f"Nhiệt độ phòng là {current_room_temp} độ C, độ ẩm {current_room_hum} phần trăm."
            current_bot_mode = "SET_MODE_3" 

        elif "mấy giờ rồi" in spoken_text:
            now = datetime.now()
            reply_text = f"Bây giờ là {now.strftime('%H')} giờ {now.strftime('%M')} phút."
            current_bot_mode = "SET_MODE_2" 

        elif "mode 5" in spoken_text or "nháy nhạc" in spoken_text:
            mode_5_active = True
            reply_text = "Đã chuyển sang chế độ nháy nhạc."
            current_bot_mode = "SET_MODE_5"
            is_awake = False 

        elif "đi ngủ đi" in spoken_text or "ngủ đi" in spoken_text:
            is_awake = False
            waiting_for_alarm = False
            reply_text = "Vâng ạ."
            current_bot_mode = "SET_MODE_0" 

        else:
            reply_text = "Sếp nói lại đi."
            current_bot_mode = "SET_MODE_1" 

    mp3_path = "response.mp3"
    raw_pcm_reply = "response.pcm"

    tts = gTTS(text=reply_text, lang="vi")
    tts.save(mp3_path)
    os.system(
        f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
    )

    if is_awake:
        last_active_time = time.time()

    if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
        resp = make_response(
            send_file(raw_pcm_reply, mimetype="application/octet-stream")
        )
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
