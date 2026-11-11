// 浏览器端图片压缩
//
// 部署在 Serverless 平台（如 Vercel）时，单次请求体上限约 4.5MB。
// 手机拍的照片动辄 5~10MB，所以上传前先在浏览器里缩小并转成 JPEG。

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('图片加载失败'))
    img.src = src
  })
}

function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(new Error('文件读取失败'))
    reader.readAsDataURL(file)
  })
}

/**
 * 把图片缩到最长边不超过 maxDimension，返回 JPEG 的 data URL。
 * 图片本来就够小（尺寸和体积都在限制内）时原样返回，避免无谓的重新编码。
 */
export async function compressImageToDataUrl(
  file: Blob,
  maxDimension = 1280,
  quality = 0.82,
  keepIfSmallerThan = 300 * 1024,
): Promise<string> {
  const original = await readAsDataUrl(file)
  let img: HTMLImageElement
  try {
    img = await loadImage(original)
  } catch {
    return original // 浏览器解不了这张图，交给服务端判断
  }

  const scale = Math.min(1, maxDimension / Math.max(img.naturalWidth, img.naturalHeight))
  if (scale === 1 && file.size <= keepIfSmallerThan) return original

  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(img.naturalWidth * scale))
  canvas.height = Math.max(1, Math.round(img.naturalHeight * scale))
  const ctx = canvas.getContext('2d')
  if (!ctx) return original
  // JPEG 没有透明通道，先铺白底
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  return canvas.toDataURL('image/jpeg', quality)
}

/** 压缩头像：最长边 512px，返回可直接放进 FormData 的 File */
export async function compressAvatar(file: File, maxDimension = 512): Promise<File> {
  // 动图压缩后会变成静态图，体积不大就保持原样
  if (file.type === 'image/gif' && file.size <= 2 * 1024 * 1024) return file
  const dataUrl = await compressImageToDataUrl(file, maxDimension, 0.85, 200 * 1024)
  if (!dataUrl.startsWith('data:image/jpeg')) return file // 没有重新编码，原样上传
  const blob = await (await fetch(dataUrl)).blob()
  const name = file.name.replace(/\.[^.]+$/, '') || 'avatar'
  return new File([blob], `${name}.jpg`, { type: 'image/jpeg' })
}
