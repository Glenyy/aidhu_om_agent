<!-- S01 占位页：仅用于验证路由与构建产物，不是产品流程。 -->
<script setup lang="ts">
const toolchain = [
  { name: 'Vue', note: '根组件与响应式' },
  { name: 'Vue Router', note: '路由注册' },
  { name: 'Element Plus', note: '组件库加载' },
  { name: 'TypeScript + vue-tsc', note: '类型检查' },
  { name: 'Vite', note: '开发与构建' },
];
</script>

<template>
  <el-card class="scaffold-card">
    <template #header>工程骨架占位页</template>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="本页只验证前端工具链，上传、进度与结果页面在 S06 实现。"
    />
    <el-table :data="toolchain" class="scaffold-card__table">
      <el-table-column prop="name" label="依赖" />
      <el-table-column prop="note" label="验证点" />
    </el-table>
  </el-card>
</template>
