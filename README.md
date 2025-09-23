# ToonToonic FFmpeg API

Сервис для автоматического монтажа видео из сцен для проекта ToonToonic.

## Функционал
- Склейка видео файлов
- Наложение аудиодорожек
- Добавление фоновой музыки
- Загрузка результата в облачное хранилище

## API Endpoints

### POST /merge-videos
Склеивает видео из массива сцен

**Request:**
```json
{
  "scenes": [
    {
      "video_url": "https://example.com/video1.mp4",
      "audio_url": "https://example.com/audio1.wav",
      "duration": 8,
      "scene_number": 1
    }
  ],
  "background_music_url": "https://example.com/music.mp3"
}
