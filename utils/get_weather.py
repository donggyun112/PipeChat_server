import datetime
from datetime import datetime, timedelta
import os
import requests
import xmltodict

def get_base_time():
    now = datetime.now()
    
    if now.minute < 40:
        now = now - timedelta(hours=1)
    
    base_date = now.strftime("%Y%m%d")
    base_time = now.strftime("%H00")
    
    return base_date, base_time

# 강수형태 코드 변환
WEATHER_CODES = {
    "0": "맑음",
    "1": "비",
    "2": "비/눈",
    "3": "눈",
    "5": "빗방울",
    "6": "빗방울눈날림",
    "7": "눈날림"
}

def get_weather(location="서울", format="celsius"):
    """
    기상청 초단기실황 API를 호출하여 현재 날씨 정보를 반환합니다.
    format 파라미터로 온도 단위(celsius 또는 fahrenheit)를 지정할 수 있습니다.
    """
    weather_api_key = os.getenv("WEATHER_API_KEY", "VliFoTTLUH2KEEuPepKt5lZzbLvdosyhYACMhWk4alvssYnrAuFbG1+V8e5QS8fbXqfLqCxMTLjm+1KwLA8sIQ==")
    
    coords = {
        "서울": {"nx": "60", "ny": "127"},
        "부산": {"nx": "98", "ny": "76"},
        "인천": {"nx": "55", "ny": "124"},
        "대구": {"nx": "89", "ny": "90"},
        "대전": {"nx": "67", "ny": "100"},
        "default": {"nx": "60", "ny": "127"}
    }
    
    location_key = "default"
    for key in coords.keys():
        if key in location:
            location_key = key
            break
    
    nx = coords[location_key]["nx"]
    ny = coords[location_key]["ny"]
    
    base_date, base_time = get_base_time()
    
    params = {
        'serviceKey': weather_api_key, 
        'pageNo': '1', 
        'numOfRows': '10', 
        'dataType': 'XML', 
        'base_date': base_date, 
        'base_time': base_time,
        'nx': nx, 
        'ny': ny
    }
    
    try:
        # 초단기실황 API 호출
        url = 'http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst'
        response = requests.get(url, params)
        response.raise_for_status()
        
        dict_data = xmltodict.parse(response.text)
        
        items = dict_data['response']['body']['items']['item']
        
        temp = "알 수 없음"
        sky = "0"
        
        for item in items:
            if item['category'] == 'T1H':
                temp = item['obsrValue']
            elif item['category'] == 'PTY':
                sky = item['obsrValue']
        
        try:
            temp_value = float(temp)
            
            if format.lower() == "fahrenheit":
                temp_value = (temp_value * 9/5) + 32
                temp = f"{temp_value:.1f}°F"
            else:
                temp = f"{temp_value:.1f}°C"
                
        except (ValueError, TypeError):
            temp = "알 수 없음"
        
        weather_condition = WEATHER_CODES.get(sky, "알 수 없음")
        
        return {
            "location": location_key,
            "conditions": weather_condition,
            "temperature": temp,
            "unit": "fahrenheit" if format.lower() == "fahrenheit" else "celsius"
        }
        
    except Exception as e:
        return {
            "location": location_key,
            "conditions": "데이터 없음",
            "temperature": "알 수 없음",
            "unit": format.lower(),
            "error": str(e)
        }

if __name__ == "__main__":
    locations = ["서울", "부산", "인천", "대구", "대전"]
    
    for loc in locations:
        weather_info = get_weather(loc)
        print(f"위치: {weather_info['location']}")
        print(f"날씨: {weather_info['conditions']}")
        print(f"온도: {weather_info['temperature']}")
        print("-" * 20)
        
    # 화씨 테스트
    weather_info = get_weather("서울", "fahrenheit")
    print(f"위치: {weather_info['location']}")
    print(f"날씨: {weather_info['conditions']}")
    print(f"온도: {weather_info['temperature']} (화씨)")
    print("-" * 20)