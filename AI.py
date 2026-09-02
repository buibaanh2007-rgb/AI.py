from datetime import datetime
import os
import time
import unicodedata

from flask import Flask, make_response, request, send_file
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
alarm_period = None  # "sáng" hoặc "chiều"

print("[Server] Đã sẵn sàng chạy theo cơ chế thu âm 2 giây tối ưu!")


def remove_accents(input_str):
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).lower()


@app.route("/")
def home():
    return "AI Speaker Server Running!"


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time, waiting_for_alarm, alarm_hour, alarm_minute, alarm_period

    current_bot_mode = "DEFAULT"

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
                return resp
        return "", 204

    # 2. Kiểm tra timeout 60 giây kể từ lần tương tác trước
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
            return resp
    else:
        text_clean = remove_accents(spoken_text)

        # KIỂM TRA ĐANG TRONG TIẾN TRÌNH ĐẶT BÁO THỨC
        if waiting_for_alarm:
            # ĐƯA LỆNH HỦY/XÓA LÊN ĐẦU TIÊN TUYỆT ĐỐI TRONG TIẾN TRÌNH
            if any(k in spoken_text for k in ["hủy báo thức", "xóa báo thức", "bỏ báo thức", "hủy lịch"]) or any(k in text_clean for k in ["hủy", "thôi", "dừng", "khong dat nua"]):
                waiting_for_alarm = False
                alarm_hour = None
                alarm_minute = None
                alarm_period = None
                reply_text = "Đã xóa báo thức đã đặt ạ."
                current_bot_mode = "SET_MODE_1" 
                print("[Server] Đã hủy báo thức theo yêu cầu.")
            else:
                # Quét trích xuất số cho giờ và phút (GIỮ LẠI GIÁ TRỊ CŨ NẾU CÂU TRƯỚC ĐÃ CÓ)
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
                        elif alarm_minute is None:
                            alarm_minute = val

                # Quét nhận diện buổi sáng hay chiều
                if any(s in text_clean for s in ["sáng", "am"]):
                    alarm_period = "sáng"
                elif any(b in text_clean for b in ["chiều", "toi", "trua", "pm", "chieu"]):
                    alarm_period = "chiều"

                # Kiểm tra thiếu thành phần nào thì hỏi đúng thành phần đó
                if alarm_hour is None:
                    reply_text = "Sếp muốn đặt lúc mấy giờ ạ?"
                    current_bot_mode = "SET_MODE_1" 
                elif alarm_minute is None:
                    reply_text = "Sếp muốn đặt phút thứ mấy ạ?"
                    current_bot_mode = "SET_MODE_1" 
                else:
                    # Nếu người dùng đã đọc đủ Giờ và Phút nhưng chưa nói sáng/chiều thì tự động gán thông minh theo khung giờ
                    if alarm_period is None:
                        alarm_period = "sáng" if alarm_hour < 12 else "chiều"

                    final_time_str = f"{alarm_hour} giờ {alarm_minute} phút {alarm_period}"
                    reply_text = f"Đã rõ, đặt báo thức lúc {final_time_str}"
                    current_bot_mode = "SET_MODE_1" 
                    print(f"[Server] Thiết lập thành công báo thức: {final_time_str}")

                    # Reset sạch sẽ toàn bộ trạng thái sau khi hoàn tất
                    waiting_for_alarm = False
                    alarm_hour = None
                    alarm_minute = None
                    alarm_period = None

        elif "đặt báo thức" in spoken_text or "báo thức" in spoken_text:
            waiting_for_alarm = True
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Sếp muốn đặt thế nào?"
            current_bot_mode = "SET_MODE_1" 
            print("[Server] Bắt đầu tiến trình đặt báo thức...")
            
        elif any(k in spoken_text for k in ["hủy báo thức", "xóa báo thức", "bỏ báo thức", "hủy lịch"]) or any(k in text_clean for k in ["huy", "xoa bao thuc", "bo bao thuc"]):
            waiting_for_alarm = False
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Đã xóa báo thức đã đặt ạ."
            current_bot_mode = "SET_MODE_1" 
            print("[Server] Đã hủy báo thức theo yêu cầu.")

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
            
        elif "mấy giờ rồi" in spoken_text or "giờ" in spoken_text:
            now = datetime.now()
            reply_text = (
                f"Bây giờ là {now.strftime('%H')} giờ {now.strftime('%M')} phút"
            )
            current_bot_mode = "SET_MODE_2" 
            
        elif "thời tiết" in spoken_text:
            current_bot_mode = "SET_MODE_3" 
            try:
                location = "HaNam"
                parts = spoken_text.split("thời tiết")
                if len(parts) > 1 and parts[1].strip() != "":
                    raw_loc = parts[1].strip()
                    raw_loc = (
                        raw_loc.replace("ở", "").replace("tại", "").strip()
                    )
                    if raw_loc != "":
                        clean_loc = remove_accents(raw_loc)
                        location = clean_loc.replace(" ", "")

                response = requests.get(
                    f"https://wttr.in/{location}?format=j1", timeout=3
                )
                if response.status_code == 200:
                    data = response.json()
                    temp = data["current_condition"][0]["temp_C"]
                    humidity = data["current_condition"][0]["humidity"]
                    
                    reply_text = f"Thời tiết {location}. nhiệt độ {temp} độ C. độ ẩm {humidity} phần trăm"
                else:
                    reply_text = "Không tìm thấy thời tiết khu vực này"
            except Exception as e:
                print(f"[Lỗi thời tiết chi tiết]: {e}")
                reply_text = "Lỗi kết nối thời tiết"
                
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
            except Exception as e:
                print(f"[Lỗi tính toán]: {e}")
                reply_text = "Em không thực hiện được phép tính này."
                
        elif "đi ngủ đi" in spoken_text or "ngủ đi" in spoken_text:
            is_awake = False
            waiting_for_alarm = False
            alarm_hour = None
            alarm_minute = None
            alarm_period = None
            reply_text = "Vâng ạ"
            current_bot_mode = "SET_MODE_0" 
            print("[Server] Trạng thái: Đã chuyển về NGỦ theo yêu cầu.")
            
        elif any(kw in spoken_text for kw in ["tắt báo thức", "dừng báo thức", "tắt chuông", "dừng chuông"]):
            reply_text = "Đã tắt báo thức ạ."
            current_bot_mode = "ALARM_STOP" 
            print("[Server] Đã nhận lệnh tắt báo thức qua giọng nói.")
            
        else:
            reply_text = "Sếp nói lại đi."
            current_bot_mode = "SET_MODE_1" 

    print(f"[Server] Phản hồi: {reply_text} | Bot-Mode: {current_bot_mode}")

    # 5. Tạo file âm thanh trả về cho ESP32 phát loa
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
        return resp
    else:
        print("[Lỗi] File PCM phản hồi bị rỗng hoặc lỗi tạo từ ffmpeg!")
        return "", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
