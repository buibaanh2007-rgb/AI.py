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

is_awake = False
last_active_time = 0
SLEEP_TIMEOUT = 60

print("[Server] Đã sẵn sàng chạy theo cơ chế tách đoạn chống kẹt socket!")

def remove_accents(input_str):
    nfkd_form = unicodedata.normalize("NFKD", input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).lower()

@app.route("/")
def home():
    return "AI Speaker Server Running!"

@app.route("/process-audio", methods=["POST"])
def process_audio():
    global is_awake, last_active_time

    if request.is_json:
        data = request.get_json()
        event_type = data.get("type", "")
        if event_type == "boot" or event_type == "connected":
            is_awake = False
            reply_text = "Ket noi server thanh cong"
            mp3_path = "response.mp3"
            raw_pcm_reply = "response.pcm"
            gTTS(text=reply_text, lang="vi").save(mp3_path)
            os.system(f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1")
            
            if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
                resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
                resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
                return resp
        return "", 204

    if is_awake and (time.time() - last_active_time > SLEEP_TIMEOUT):
        is_awake = False

    audio_data = request.data
    if len(audio_data) < 1000:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp

    raw_pcm_path = "input_temp.pcm"
    wav_path = "input_temp.wav"
    with open(raw_pcm_path, "wb") as f:
        f.write(audio_data)

    os.system(f"ffmpeg -y -f s16le -ar 16000 -ac 1 -i {raw_pcm_path} {wav_path} > /dev/null 2>&1")

    spoken_text = ""
    try:
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            spoken_text = recognizer.recognize_google(audio, language="vi-VN").lower()
            print(f"[Server] Nghe được: '{spoken_text}'")
    except:
        resp = make_response("", 204)
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp

    raw_pcm_reply = "response.pcm"

    if not is_awake:
        if "xin chào" in spoken_text or "thức dậy" in spoken_text:
            is_awake = True
            reply_text = "Chao sep, sep can giup gi a?"
        else:
            resp = make_response("", 204)
            resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
            return resp
    else:
        if "nhiệt độ" in spoken_text or "nhiệt" in spoken_text:
            reply_text = "Nhiet do 34 do C"
        elif "mấy giờ rồi" in spoken_text or "giờ" in spoken_text:
            now = datetime.now()
            reply_text = f"Bay gio la {now.strftime('%H')} gio {now.strftime('%M')} phut"
        elif "thời tiết" in spoken_text:
            try:
                location = "HaNam"
                parts = spoken_text.split("thời tiết")
                if len(parts) > 1 and parts[1].strip() != "":
                    raw_loc = parts[1].strip().replace("ở", "").replace("tại", "").strip()
                    if raw_loc != "":
                        location = remove_accents(raw_loc).replace(" ", "")

                # Gọi API lấy thời tiết cực nhanh
                response = requests.get(f"https://wttr.in/{location}?format=j1", timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    temp = data["current_condition"][0]["temp_C"]
                    humidity = data["current_condition"][0]["humidity"]
                    
                    # TÁCH LÀM 3 PHẦN RIÊNG BIỆT HOÀN TOÀN, CÓ CHÈN KHOẢNG TRẮNG ĐỆM ĐỂ GTTs ĐỌC CÓ NGẮT NHỊP RÕ RÀNG
                    text_parts = [
                        f"Thoi tiet {location} la.",
                        f"Nhiet do la {temp} do C.",
                        f"Do am la {humidity} phan tram."
                    ]
                else:
                    text_parts = ["Khong tim thay thoi tiet khu vuc nay."]
            except:
                text_parts = ["Loi ket nối thoi tiet."]

            # Render từng câu ra file pcm riêng và nối chúng lại với 1 khoảng im lặng siêu mượt giữa các câu
            combined_files = []
            for i, text in enumerate(text_parts):
                p_mp3 = f"part_{i}.mp3"
                p_pcm = f"part_{i}.pcm"
                gTTS(text=text, lang="vi").save(p_mp3)
                os.system(f"ffmpeg -y -i {p_mp3} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {p_pcm} > /dev/null 2>&1")
                combined_files.append(p_pcm)

            # Tạo 1 đoạn im lặng dài khoảng 1.5 giây (16000 * 2 * 1.5 = 48000 bytes)
            silence_chunk = b'\x00' * 48000

            with open(raw_pcm_reply, "wb") as outfile:
                for idx, pcm_file in enumerate(combined_files):
                    if os.path.exists(pcm_file):
                        with open(pcm_file, "rb") as infile:
                            outfile.write(infile.read())
                    # Chèn khoảng im lặng giữa các câu để tạo nhịp nghỉ rõ ràng
                    if idx < len(combined_files) - 1:
                        outfile.write(silence_chunk)

            if is_awake:
                last_active_time = time.time()

            if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
                resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
                resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
                return resp
            else:
                return "", 500

        elif "ngủ đi" in spoken_text or "tắt đi" in spoken_text:
            is_awake = False
            reply_text = "Van a"
        else:
            reply_text = "Toi khong hieu yeu cau nay."

    mp3_path = "response.mp3"
    gTTS(text=reply_text, lang="vi").save(mp3_path)
    os.system(f"ffmpeg -y -i {mp3_path} -f s16le -acodec pcm_s16le -ar 16000 -ac 1 {raw_pcm_reply} > /dev/null 2>&1")

    if is_awake:
        last_active_time = time.time()

    if os.path.exists(raw_pcm_reply) and os.path.getsize(raw_pcm_reply) > 0:
        resp = make_response(send_file(raw_pcm_reply, mimetype="application/octet-stream"))
        resp.headers["Bot-State"] = "THUC" if is_awake else "NGU"
        return resp
    else:
        return "", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
