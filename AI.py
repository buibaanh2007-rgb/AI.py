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

print("[Server] Đã sẵn sàng chạy theo cơ chế thu âm 5 giây tuần tự!")


def remove_accents(input_str):
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).lower()


@app.route("/")
def home():
    return "AI Speaker Server Running!"


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time

    # 1. Xử lý sự kiện hệ thống (boot, connected từ ESP32)
    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")
        if event_type == "boot" or event_type == "connected":
            is_awake = False
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
                return resp
        return "", 204

    # 2. Kiểm tra timeout 60 giây kể từ lần tương tác trước
    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False
        print("[Server] Đã quá 60 giây không có tương tác, tự động chuyển về chế độ NGỦ.")

    # 3. Nhận audio thô 5 giây từ ESP32
    audio_data = request.data
    if len(audio_data) < 1000:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
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
        print("[Server] Không nghe rõ nội dung hoặc khoảng lặng 5 giây.")
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    except sr.RequestError as e:
        print(f"[Server] Lỗi kết nối Google STT: {e}")
        return "", 500

    # 4. Phân rã logic theo trạng thái THỨC hay NGỦ
    if not is_awake:
        # Đang ngủ: Chỉ bắt từ khóa đánh thức
        wake_words = ["xin chào", "chào", "chào bot", "ê bot", "bật dậy", "dậy đi"]
        if any(word in spoken_text for word in wake_words):
            is_awake = True
            reply_text = "Chào sếp, sếp cần giúp gì ạ?"
            print("[Server] Trạng thái: ĐÃ THỨC.")
        else:
            resp = make_response("", 204)
            resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
            return resp
    else:
        # Đang thức: Xử lý các câu lệnh chức năng
        if "nhiệt độ" in spoken_text or "nhiệt" in spoken_text:
            reply_text = "nhiệt độ 34 độ C"
        elif "mấy giờ rồi" in spoken_text or "giờ" in spoken_text:
            now = datetime.now()
            reply_text = (
                f"Bây giờ là {now.strftime('%H')} giờ {now.strftime('%M')} phút"
            )
        elif "thời tiết" in spoken_text:
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

                # Gọi API thời tiết với timeout an toàn
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
                reply_text = "Loi ket noi thời tiết"
        elif any(op in spoken_text for op in ["cộng", "trừ", "nhân", "chia", "x", "+", "-", "*", "/"]):
            try:
                # Thay thế các từ khóa và dấu x thành toán tử chuẩn của Python
                expr = (spoken_text
                        .replace("cộng", "+")
                        .replace("trừ", "-")
                        .replace("nhân", "*")
                        .replace(" x ", "*")
                        .replace(" x", "*")
                        .replace("x ", "*")
                        .replace("chia", "/"))
                
                # Lọc lại chỉ giữ các ký tự toán học, số và khoảng trắng để đảm bảo an toàn khi dùng eval
                allowed_chars = set("0123456789+-*/. ")
                if all(c in allowed_chars for c in expr):
                    result = eval(expr)
                    # Làm tròn kết quả nếu là số thập phân
                    if isinstance(result, float) and not result.is_integer():
                        result = round(result, 2)
                    reply_text = f"Kết quả bằng {result}"
                else:
                    reply_text = "Em chỉ tính toán với các con số đơn giản thôi ạ."
            except Exception as e:
                print(f"[Lỗi tính toán]: {e}")
                reply_text = "Em không thực hiện được phép tính này."
        elif "đi ngủ đi" in spoken_text or "tắt đi" in spoken_text:
            is_awake = False
            reply_text = "Vâng ạ"
            print("[Server] Trạng thái: Đã chuyển về NGỦ theo yêu cầu.")
        else:
            reply_text = "Tôi không hiểu yêu cầu này."

    # Cho server "suy nghĩ" nhẹ 1.5 giây để gom data và render ổn định
    print("[Server] Đang tổng hợp dữ liệu...")
    time.sleep(1.5)

    print(f"[Server] Phản hồi: {reply_text}")

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

    # Kiểm tra kĩ file PCM có thực sự tồn tại và có dung lượng > 0 hay không
    if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
        resp = make_response(
            send_file(raw_pcm_reply, mimetype="application/octet-stream")
        )
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    else:
        print("[Lỗi] File PCM phản hồi bị rỗng hoặc lỗi tạo từ ffmpeg!")
        return "", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
