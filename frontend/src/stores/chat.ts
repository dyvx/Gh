import { defineStore } from 'pinia'
import { ref } from 'vue'
import api, { uploadApi, refreshAccessToken, forceLogout } from '@/utils/http'
import { compressAvatar } from '@/utils/image'
import { useAuthStore } from '@/stores/auth'
import type { Character, Conversation, Message, Memory } from '@/types/models'

// HTTP (非 HTTPS) 下 crypto.randomUUID 不可用，使用 fallback
function genUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

export const useChatStore = defineStore('chat', () => {
  const characters = ref<Character[]>([])
  const currentCharacter = ref<Character | null>(null)
  const conversations = ref<Conversation[]>([])
  const currentConversation = ref<Conversation | null>(null)
  const messages = ref<Message[]>([])
  const isStreaming = ref(false)
  const streamingContent = ref('')
  const streamingComplete = ref(false) // 用于过渡动画
  // 进行中的聊天请求（HTTP 流式）。切换角色 / 离开页面时全部中止。
  const activeStreams = new Set<AbortController>()
  const emotionState = ref<{ primary: string; intensity: number; valence: number } | null>(null)

  // 记忆
  const memories = ref<Memory[]>([])

  async function loadCharacters() {
    const res = await api.get<Character[]>('/characters')
    characters.value = res.data
  }

  async function selectCharacter(characterId: string) {
    let char = characters.value.find(c => c.id === characterId)
    if (!char) {
      // 角色不在 store 中（如直接刷新页面），从 API 加载
      try {
        const res = await api.get<Character>(`/characters/${characterId}`)
        char = res.data
        if (!characters.value.find(c => c.id === char!.id)) {
          characters.value.push(char)
        }
      } catch {
        throw new Error('角色不存在')
      }
    }
    if (!char) throw new Error('角色不存在')
    currentCharacter.value = char
    // 重置流式状态，防止上次未完成就退出导致加载条卡死
    isStreaming.value = false
    streamingContent.value = ''
    streamingComplete.value = false
    emotionState.value = null

    // 加载该角色所有会话作为短期记忆
    const convRes = await api.get<Conversation[]>('/conversations', {
      params: { character_id: char.id },
    })
    if (convRes.data.length > 0) {
      conversations.value = convRes.data
      currentConversation.value = convRes.data[0]
    } else {
      const res = await api.post('/conversations', null, {
        params: { character_id: char.id },
      })
      conversations.value = [res.data]
      currentConversation.value = res.data
    }
    messages.value = []
    abortActiveStreams()
    await loadMessages()
    await loadMemories(characterId)

    // 检测中断：最后一条是用户发的但没收到 AI 回复 → 自动重发
    const msgs = messages.value
    if (msgs.length > 0 && msgs[msgs.length - 1].role === 'user') {
      const last = msgs.pop()!
      // 库里已有这条消息，先删掉再重发，避免重复
      if (last.id && currentConversation.value) {
        try { await api.delete(`/conversations/${currentConversation.value.id}/messages/${last.id}`) } catch {}
      }
      const text = last.content === '[图片]' && last.content_type === 'image' ? '' : last.content
      sendMessage(text, last.metadata?.image_base64)
    }
  }

  async function createCharacter(data: {
    name: string
    description: string
    system_prompt?: string
    personality_profile?: Record<string, any> | null
  }) {
    const res = await api.post<Character>('/characters', data)
    characters.value.push(res.data)
    return res.data
  }

  async function generateCharacterProfile(description: string): Promise<Record<string, any>> {
    const res = await api.post<{ personality_profile: Record<string, any> }>(
      '/characters/generate',
      { user_description: description },
      { timeout: 120000 },
    )
    return res.data.personality_profile
  }

  async function analyzeChatLogs(chatText: string, currentProfile?: Record<string, any> | null) {
    const res = await api.post<{
      enhanced_profile: Record<string, any>
    }>('/characters/analyze-chat', {
      chat_text: chatText,
      current_profile: currentProfile || null,
    }, { timeout: 120000 })
    return res.data
  }

  async function uploadCharacterDocuments(characterId: string, files: File[]) {
    for (const file of files) {
      const form = new FormData()
      form.append('file', file)
      await uploadApi.post(`/characters/${characterId}/documents`, form)
    }
  }

  async function updateCharacter(id: string, data: { name?: string; description?: string; system_prompt?: string; personality_profile?: Record<string, any> | null }) {
    const res = await api.patch<Character>(`/characters/${id}`, data)
    const idx = characters.value.findIndex(c => c.id === id)
    if (idx >= 0) characters.value[idx] = res.data
    if (currentCharacter.value?.id === id) currentCharacter.value = res.data
    return res.data
  }

  async function deleteCharacter(id: string) {
    await api.delete(`/characters/${id}`)
    characters.value = characters.value.filter(c => c.id !== id)
  }

  async function uploadAvatar(characterId: string, file: File) {
    const form = new FormData()
    form.append('file', await compressAvatar(file))
    const res = await uploadApi.post<Character>(`/characters/${characterId}/avatar`, form)
    // 更新列表和当前角色中的头像
    const idx = characters.value.findIndex(c => c.id === characterId)
    if (idx >= 0) characters.value[idx] = res.data
    if (currentCharacter.value?.id === characterId) currentCharacter.value = res.data
    characterAvatarVersion.value[characterId] = (characterAvatarVersion.value[characterId] || 0) + 1
    return res.data
  }

  async function loadMessages() {
    if (conversations.value.length === 0) return
    const results = await Promise.all(
      conversations.value.map(conv =>
        api.get<Message[]>(`/conversations/${conv.id}/messages`)
      )
    )
    const allMessages = results.flatMap(r => r.data)
    allMessages.sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
    messages.value = allMessages
  }

  // ── 记忆 ──

  async function loadMemories(characterId?: string) {
    const cid = characterId || currentCharacter.value?.id
    if (!cid) return
    try {
      const res = await api.get<Memory[]>('/memories', { params: { character_id: cid } })
      memories.value = res.data
    } catch { /* ignore */ }
  }

  async function updateMemory(id: string, data: { content?: string; importance?: number }) {
    await api.patch(`/memories/${id}`, data)
    const idx = memories.value.findIndex(m => m.id === id)
    if (idx >= 0) {
      if (data.content !== undefined) memories.value[idx].content = data.content
      if (data.importance !== undefined) memories.value[idx].importance = data.importance
    }
  }

  async function deleteMemory(id: string) {
    await api.delete(`/memories/${id}`)
    memories.value = memories.value.filter(m => m.id !== id)
  }

  const characterAvatarVersion = ref<Record<string, number>>({})

  function getAvatarUrl(avatarUrl: string | null | undefined, characterId?: string): string {
    if (!avatarUrl) return ''
    const v = characterId ? (characterAvatarVersion.value[characterId] || 0) : 0
    return v > 0 ? `${avatarUrl}?v=${v}` : avatarUrl
  }

  const consolidating = ref(false)
  async function consolidateMemories(characterId?: string | null) {
    const cid = characterId || currentCharacter.value?.id
    if (!cid) return
    consolidating.value = true
    try {
      const res = await api.post('/memories/consolidate', null, { params: { character_id: cid } })
      if (res.data.consolidated) {
        await loadMemories(cid)
      }
      return res.data
    } finally {
      consolidating.value = false
    }
  }

  // ── 聊天（HTTP 流式）──
  //
  // 每发一条消息就是一次 POST /conversations/{id}/chat，服务端以 NDJSON
  // （每行一个 JSON 事件）流式返回。相比 WebSocket 长连接，这种一问一答的
  // 方式在 Serverless 平台（Vercel 等）上也能稳定工作，也不需要断线重连。

  function resetStreamingState() {
    isStreaming.value = false
    streamingContent.value = ''
    streamingComplete.value = false
  }

  function abortActiveStreams() {
    for (const controller of activeStreams) controller.abort()
    activeStreams.clear()
  }

  function pushAssistantText(content: string, metadata: Record<string, any> | null = null) {
    messages.value.push({
      id: genUUID(),
      role: 'assistant',
      content,
      content_type: 'text',
      metadata,
      emotion_label: null,
      created_at: new Date().toISOString(),
    })
  }

  // 处理服务端事件。localUserMsgId 是本次请求对应的那条本地用户消息。
  function handleChatEvent(data: any, localUserMsgId: string) {
    if (data.type === 'user_saved') {
      // 用户消息已入库：把本地临时 id 换成数据库 id，之后撤回 / 重试才能真正删掉它
      const local = messages.value.find(m => m.id === localUserMsgId)
      if (local && data.id) local.id = data.id
    } else if (data.type === 'chunk') {
      if (!isStreaming.value) isStreaming.value = true
      streamingContent.value += data.content
    } else if (data.type === 'done') {
      streamingComplete.value = true
      const hasImage = !!data.image_url
      const hasText = data.full_reply && data.full_reply.trim()
      if (hasText || hasImage) {
        messages.value.push({
          id: data.id || genUUID(),
          role: 'assistant',
          content: data.full_reply || '',
          content_type: hasImage ? 'image' : 'text',
          metadata: hasImage ? { image_url: data.image_url, prompt: data.image_prompt } : null,
          emotion_label: null,
          created_at: new Date().toISOString(),
        })
      }
      if (data.emotion_state) {
        emotionState.value = data.emotion_state
      }
      // 过渡动画后清除流式内容
      setTimeout(() => {
        streamingContent.value = ''
        streamingComplete.value = false
      }, 300)
      isStreaming.value = false
    } else if (data.type === 'image') {
      messages.value.push({
        id: data.id || genUUID(),
        role: 'assistant',
        content: data.prompt || '',
        content_type: 'image',
        metadata: { image_url: data.url, prompt: data.prompt },
        emotion_label: null,
        created_at: new Date().toISOString(),
      })
      // 收到图片后清除流式状态
      resetStreamingState()
    } else if (data.type === 'ai_error') {
      resetStreamingState()
      const errorMessages: Record<string, string> = {
        invalid_key: 'API Key 无效，请前往个人中心重新设置',
        insufficient_balance: 'API 余额不足，请充值或更换 Key',
        server_error: 'AI 服务暂时异常，请稍后重试',
        unknown: 'AI 调用失败，请稍后重试',
      }
      pushAssistantText(errorMessages[data.code] || data.message || 'AI 调用失败', { error_code: data.code })
    } else if (data.type === 'error') {
      resetStreamingState()
      pushAssistantText('呜…刚才好像出了点问题 (′；ω；`) 能再说一遍吗？')
      console.error(data.message)
    }
    // 'ping' 是服务端的保活心跳，忽略即可
  }

  async function streamChat(conversationId: string, text: string, image: string | null | undefined, localUserMsgId: string) {
    const controller = new AbortController()
    activeStreams.add(controller)
    let gotReply = false // 是否收到过任何「结果」事件

    const request = () =>
      fetch(`/api/v1/conversations/${conversationId}/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
        },
        body: JSON.stringify({ message: text, image: image || null }),
        signal: controller.signal,
      })

    try {
      let res = await request()
      if (res.status === 401) {
        // access token 过期（有效期只有 15 分钟）：刷新后重试一次
        const refreshed = await refreshAccessToken()
        if (!refreshed) {
          forceLogout()
          return
        }
        res = await request()
        if (res.status === 401) {
          forceLogout()
          return
        }
      }
      if (!res.ok || !res.body) {
        let detail = ''
        try { detail = (await res.json()).detail || '' } catch {}
        throw new Error(detail || `HTTP ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      const consume = (line: string) => {
        if (!line.trim()) return
        let data: any
        try { data = JSON.parse(line) } catch { return }
        if (data.type !== 'ping' && data.type !== 'user_saved') gotReply = true
        handleChatEvent(data, localUserMsgId)
      }
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let newline: number
        while ((newline = buffer.indexOf('\n')) >= 0) {
          consume(buffer.slice(0, newline))
          buffer = buffer.slice(newline + 1)
        }
      }
      consume(buffer + decoder.decode())

      if (!gotReply) throw new Error('连接中断，没有收到回复')
    } catch (err) {
      if (controller.signal.aborted) return // 用户切换角色 / 离开页面，属于正常中止
      console.error('聊天请求失败:', err)
      if (!gotReply) {
        handleChatEvent({ type: 'error', message: String(err) }, localUserMsgId)
      } else {
        resetStreamingState()
      }
    } finally {
      activeStreams.delete(controller)
    }
  }

  // 保留原有的方法名，页面组件不需要改动。HTTP 模式下不需要预先建立连接。
  function connectWebSocket() {}

  function disconnectWebSocket() {
    abortActiveStreams()
    resetStreamingState()
  }

  function sendMessage(text: string, image?: string | null) {
    const auth = useAuthStore()
    if (!auth.hasApiKey) {
      auth.promptApiKey()
      return
    }
    if (!currentConversation.value) {
      console.error('没有活跃的对话')
      return
    }
    // 先将用户消息立即显示在 UI
    const localId = genUUID()
    messages.value.push({
      id: localId,
      role: 'user',
      content: text || '[图片]',
      content_type: image ? 'image' : 'text',
      metadata: image ? { image_base64: image } : null,
      emotion_label: null,
      created_at: new Date().toISOString(),
    })
    isStreaming.value = true
    streamingContent.value = ''
    streamingComplete.value = false

    void streamChat(currentConversation.value.id, text, image, localId)
  }

  async function recallMessage(msgId: string) {
    const idx = messages.value.findIndex(m => m.id === msgId)
    if (idx < 0) return
    const msg = messages.value[idx]
    // 撤回用户消息时，同时删除紧随的 AI 回复
    if (msg.role === 'user') {
      const next = messages.value[idx + 1]
      if (next && next.role === 'assistant') {
        messages.value.splice(idx, 2)
        if (next.id) {
          try { await api.delete(`/conversations/${currentConversation.value!.id}/messages/${next.id}`) } catch {}
        }
      } else {
        messages.value.splice(idx, 1)
      }
    } else {
      messages.value.splice(idx, 1)
    }
    if (msg.id) {
      try { await api.delete(`/conversations/${currentConversation.value!.id}/messages/${msg.id}`) } catch {}
    }
  }

  function retryMessage(aiMsgId: string) {
    const idx = messages.value.findIndex(m => m.id === aiMsgId)
    if (idx < 1) return
    // 找到该 AI 回复前面的用户消息
    const prev = messages.value[idx - 1]
    if (prev.role !== 'user') return
    const text = prev.content
    const image = prev.metadata?.image_base64 || null
    const prevId = prev.id
    // 从 DB 删除原用户消息和 AI 回复
    if (prevId) {
      try { api.delete(`/conversations/${currentConversation.value!.id}/messages/${prevId}`) } catch {}
    }
    if (aiMsgId) {
      try { api.delete(`/conversations/${currentConversation.value!.id}/messages/${aiMsgId}`) } catch {}
    }
    // 移除本地原用户消息和 AI 回复
    messages.value.splice(idx - 1, 2)
    // 重发用户消息
    sendMessage(text, image)
  }

  return {
    characters, currentCharacter, conversations, currentConversation,
    messages, isStreaming, streamingContent, streamingComplete, emotionState,
    memories,
    loadCharacters, selectCharacter, createCharacter, generateCharacterProfile,
    analyzeChatLogs, uploadCharacterDocuments,
    loadMessages, sendMessage, connectWebSocket, disconnectWebSocket, uploadAvatar,
    updateCharacter, deleteCharacter, loadMemories, updateMemory, deleteMemory,
    consolidating, consolidateMemories,
    getAvatarUrl,
    recallMessage, retryMessage,
  }
})
