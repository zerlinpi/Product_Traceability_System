<script setup lang="ts">
/**
 * 主图 upload field. The picture is uploaded as soon as it is chosen and the
 * returned same-origin URL becomes the column value, so saving the product
 * only persists a reference.
 */
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { PRODUCT_IMAGE_MAX_BYTES, safeImageUrl } from '../utils'

defineOptions({
  name: 'ProductImageField',
})

const props = defineProps<{
  column: string
}>()

const model = defineModel<string>({ default: '' })

const fileInput = ref<HTMLInputElement>()
const uploading = ref(false)
const previewUrl = computed(() => safeImageUrl(model.value))

function resetInput() {
  if (fileInput.value) {
    fileInput.value.value = ''
  }
}

function chooseFile() {
  if (!uploading.value) {
    fileInput.value?.click()
  }
}

async function onFileChange() {
  const file = fileInput.value?.files?.[0]
  if (!file) {
    return
  }
  if (file.size > PRODUCT_IMAGE_MAX_BYTES) {
    notifyError('图片不能超过 5 MB', '图片上传失败')
    resetInput()
    return
  }
  uploading.value = true
  try {
    const uploaded = await catalogApi.uploadProductImage(file)
    model.value = uploaded.url
    notifySuccess('图片已上传', '保存产品后生效')
  }
  catch (error) {
    notifyError(error, '图片上传失败')
  }
  finally {
    uploading.value = false
    resetInput()
  }
}

function clearImage() {
  model.value = ''
  resetInput()
}
</script>

<template>
  <div class="pts-image-field">
    <div class="pts-image-preview">
      <img v-if="previewUrl" :src="previewUrl" :alt="props.column">
      <span v-else class="pts-muted text-xs">未上传</span>
    </div>
    <div class="flex flex-col gap-2 items-start">
      <input
        ref="fileInput"
        type="file"
        class="hidden"
        accept="image/png,image/jpeg,image/gif,image/webp"
        tabindex="-1"
        :aria-label="props.column"
        @change="onFileChange"
      >
      <div class="flex flex-wrap gap-2 items-center">
        <ElButton size="small" :loading="uploading" :aria-label="`上传${props.column}`" @click="chooseFile">
          <template #icon>
            <FaIcon name="i-ri:upload-2-line" />
          </template>
          {{ model ? '更换图片' : '上传图片' }}
        </ElButton>
        <ElButton v-if="model" size="small" link type="danger" :disabled="uploading" @click="clearImage">
          移除图片
        </ElButton>
      </div>
      <span class="pts-muted text-xs">支持 PNG / JPG / GIF / WEBP，最大 5 MB，最长边不超过 10000 像素</span>
    </div>
  </div>
</template>
