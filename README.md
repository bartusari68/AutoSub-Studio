# AutoSub Studio v2.2

Kendi scriptlerimizle çalışan, watermark/logosuz yerel altyazı pipeline'ı.

## Akış

MP4/MOV/MKV → faster-whisper large-v3 → kelime başlangıç/bitiş zamanları → akıllı caption bölme → kaynak SRT → NLLB-200 çeviri → hedef SRT + ASS.

## Çıktılar

- `video.tr.source.srt` — kaynak dil altyazısı
- `video.en.srt` — çevrilmiş SRT
- `video.en.ass` — sabit stil içeren ASS
- `video.words.json` — her kelimenin start/end zamanı
- `video.timeline.json` — caption start_ms/end_ms + kaynak + çeviri

## Sabit stil

- Arial
- Font size 15
- Box per line
- Outline 0.5
- Shadow 0
- Bottom-center
- Left/Right margin 10
- Vertical margin 5
- Max 2 satır
- Yaklaşık 42 karakter/satır

ASS PlayRes 512x288 olarak tutulur; 1080p videoda Subtitle Edit benzeri ölçeklenir.

## Kurulum

1. `INSTALL_CORE.bat`
2. `INSTALL_TRANSLATION.bat`
3. NVIDIA GPU için `CUDA_SETUP.bat`
4. İstersen `CHECK_CUDA.bat`
5. `START.bat`

İlk kullanımda Whisper modeli; ilk çeviride NLLB modeli internetten indirilir. Sonrasında cache'den kullanılır.

## VAD neden varsayılan kapalı?

Sessiz/uzak/düşük seviyeli konuşmaların yanlışlıkla atlanması riskini azaltmak için. Çok gürültülü videolarda açılabilir.

## Zaman hassasiyeti

Program faster-whisper word timestamps kullanır ve değerleri SRT'de milisaniyeye yuvarlar. `words.json` orijinal float zamanları saklar. Bu sürüm forced-alignment değildir; ileride WhisperX precision backend eklenebilir.


## v2.1 Final Video Modları

Uygulama artık altyazıları ürettikten sonra final video da oluşturabilir.

### 1. MP4 sabit stil - kaynak boyutuna yakın (2-pass)

- ASS görüntünün üzerine kalıcı olarak basılır.
- Arial 15 / Box per line / Outline 0.5 / Shadow 0 ayarı sabit görünür.
- Video yeniden encode edilir.
- Ses `copy` edilir.
- Kaynak dosyanın byte boyutu ve süresi ölçülür.
- İki geçişli encode ile final dosya boyutu kaynağa yakın hedeflenir.
- Örnek: 760 MB kaynak → yaklaşık aynı büyüklükte final MP4 hedeflenir.

Bu, bit seviyesinde kalite kaybı sıfır anlamına gelmez. Burn-in yapılan MP4'te yeniden encode teknik olarak zorunludur.

### 2. MP4 sabit stil - maksimum görsel kalite (CRF 14)

- Stil kalıcıdır.
- CPU `libx264` / kaynak HEVC ise `libx265`
- `preset slow`
- `CRF 14`
- Ses `copy`
- Dosya boyutu kaynaktan büyük veya küçük olabilir.

### 3. MKV sabit ASS stil - kalite kaybı 0

- Video `copy`
- Ses `copy`
- ASS altyazı ayrı track
- Stil korunur.
- Video bitstream'i değişmez.
- Çıktı MKV'dir.

### 4. MP4 soft subtitle - kalite kaybı 0

- Video/ses `copy`
- MP4 içinde `mov_text` altyazı
- Kalite kaybı yoktur.
- ASS'in Box per line gibi gelişmiş stili korunmaz.

### Neden MKV -> MP4 ile stili koruyamıyoruz?

MKV, ASS stilini doğal olarak taşıyabilir. MP4'ün standart altyazı track'i ASS'in tüm stil özelliklerini
taşımaz. MKV'yi MP4'e yalnızca `copy` ile dönüştürmek, sabit ASS görünümünü MP4 içinde garanti etmez.
Stilin MP4'te her oynatıcıda aynı görünmesi için ASS'i görüntüye burn-in etmek gerekir; bu da video
yeniden encode demektir.

Bu nedenle v2.1'de istenirse önce kalite kayıpsız MKV arşivi oluşturulur, ardından ayrıca yüksek kaliteli
veya kaynak boyutuna yakın sabit-stilli MP4 üretilir.


## v2.2 Final Codec Standard

Burn-in MP4 outputs are now always encoded as:

- Container: MP4
- Video codec: H.264 / AVC
- Encoder: libx264
- Pixel format: yuv420p
- Audio: copy
- Preset: slow

The source codec no longer changes the final MP4 codec. H.264, H.265/HEVC, AV1, VP9,
MPEG-4 or another supported input will all produce H.264/AVC MP4 for the fixed-style
final video modes.

The lossless MKV archive mode still copies the original video and audio streams without
re-encoding.
