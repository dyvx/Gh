import { ref } from 'vue'
import { compressImageToDataUrl } from '@/utils/image'

export function useImageUpload(maxSizeMB = 20) {
  const selectedImage = ref<string | null>(null)
  const previewUrl = ref<string | null>(null)
  const fileInput = ref<HTMLInputElement | null>(null)

  function selectFile() {
    fileInput.value?.click()
  }

  function onFileSelected(event: Event) {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    processFile(file)
    input.value = ''
  }

  function onPaste(event: ClipboardEvent) {
    const items = event.clipboardData?.items
    if (!items) return
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile()
        if (file) processFile(file)
        break
      }
    }
  }

  async function processFile(file: File) {
    if (file.size > maxSizeMB * 1024 * 1024) {
      alert(`图片大小不能超过 ${maxSizeMB}MB`)
      return
    }
    try {
      // 发送前先压缩：聊天图片随消息以 base64 发送，原图很容易超过服务端的请求体上限
      const dataUrl = await compressImageToDataUrl(file)
      clearImage()
      selectedImage.value = dataUrl
      // 压缩完成后才显示预览，保证「看得到预览」时图片一定已经可以发送
      previewUrl.value = URL.createObjectURL(file)
    } catch {
      clearImage()
      alert('图片处理失败，请换一张试试')
    }
  }

  function clearImage() {
    if (previewUrl.value) {
      URL.revokeObjectURL(previewUrl.value)
    }
    selectedImage.value = null
    previewUrl.value = null
  }

  return { selectedImage, previewUrl, fileInput, selectFile, onFileSelected, onPaste, clearImage }
}
