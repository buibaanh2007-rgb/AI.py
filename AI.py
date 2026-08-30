import requests
from datetime import datetime
from flask import Flask, request, send_file, make_response
from gtts import gTTS
import os
import speech_recognition as sr
import time

app = Flask(__name__)
recognizer = sr.Recognizer()

# Biến toàn cục quản lý trạng thái thức/ngủ và timeout 10 giây
is_awake = False
last_active_time = 0
SLEEP_TIMEOUT = 20

print("[Server] Đã sẵn sàng chạy theo yêu cầu mới!")


@app.route("/")
def home():
    return "AI Speaker Server Running!"


@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time

    # Xử lý sự kiện hệ thống (như lúc boot/kết nối từ ESP32)
    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")

        reply_text = ""
        if event_type == "boot" or event_type == "connected":
            reply_text = "Kết nối server thành công"
            # Sau khi khởi động thành công, hệ thống đưa về trạng thái NGỦ để chờ lệnh "xin chào"
            is_awake = False

        if reply_text:
            print(f"[Server] Sự kiện hệ thống - Phản hồi: {reply_text}")
            mp3_path = "response.mp3"
            raw_pcm_reply = "response.pcm"

            tts = gTTS(text=reply_text, lang="vi")
            tts.save(mp3_path)
            os.system(
                f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
            )

            if os.path.exists(raw_pcm_reply):
                resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
                resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
                return resp

        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp

    # Kiểm tra xem đã quá 10 giây không tương tác hay chưa để tự động cho ngủ lại
    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False
        print("[Server] Đã quá 10 giây không có lệnh, chuyển về chế độ NGỦ.")

    # Xử lý luồng âm thanh thô do ESP32 gửi lên khi thu âm
    audio_data = request.data
    if len(audio_data) < 1000:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp

    raw_pcm_path = "input_temp.pcm"
    wav_path = "input_temp.wav"

    with open(raw_pcm_path, "wb") as f:
        f.write(audio_data)

    # Chuyển đổi PCM sang WAV bằng ffmpeg
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
        print("[Server] Không nghe rõ nội dung.")
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    except sr.RequestError as e:
        print(f"[Server] Lỗi kết nối Google STT: {e}")
        return "", 500

    reply_text = ""

    # 1. TRẠNG THÁI NGỦ: Chỉ chờ từ khóa "xin chào" hoặc "chào"
    if not is_awake:
        if "xin chào" in spoken_text or "chào" in spoken_text:
            is_awake = True
            last_active_time = time.time()  # Đánh dấu thời điểm vừa thức dậy
            reply_text = "Chào sếp, sếp cần giúp gì ạ?"
            print("[Server] Trạng thái: ĐÃ THỨC.")
        else:
            # Đang ngủ mà nói câu khác thì lờ đi, không phản hồi
            resp = make_response("", 204)
            resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
            return resp

    # 2. TRẠNG THÁI THỨC: Các lệnh khác được phép thực hiện và gia hạn thêm 10 giây
    else:
        last_active_time = time.time()  # Làm mới mốc 10 giây khi có lệnh mới

        if "nhiệt độ" in spoken_text or "nhiệt" in spoken_text:
            reply_text = "nhiệt độ 34 độ C"
        elif "mấy giờ rồi" in spoken_text or "giờ" in spoken_text:
            now = datetime.now()
            current_hour = now.strftime("%H")
            current_minute = now.strftime("%M")
            reply_text = f"Bây giờ là {current_hour} giờ {current_minute} phút"
        elif "thời tiết" in spoken_text:
            try:
                response = requests.get("https://wttr.in/HaNam?format=j1", timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    temp = data['current_condition'][0]['temp_C']
                    humidity = data['current_condition'][0]['humidity']
                    reply_text = f"Nhiệt độ Hà Nam là {temp} độ C và độ ẩm là {humidity} phần trăm"
                else:
                    reply_text = "Không lấy được dữ liệu thời tiết"
            except Exception as e:
                print(f"[Lỗi thời tiết chi tiết]: {e}")
                reply_text = "Lỗi kết nối thời tiết"
        elif "ngủ đi" in spoken_text or "tắt đi" in spoken_text:
            is_awake = False
            reply_text = "Tôi đi ngủ đây."
        else:
            reply_text = "Tôi không hiểu yêu cầu này."

    print(f"[Server] Phản hồi: {reply_text}")

    mp3_path = "response.mp3"
    raw_pcm_reply = "response.pcm"

    tts = gTTS(text=reply_text, lang="vi")
    tts.save(mp3_path)

    os.system(
        f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1"
    )

    if os.path.exists(raw_pcm_reply):
        resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    else:
        return "", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True) 
