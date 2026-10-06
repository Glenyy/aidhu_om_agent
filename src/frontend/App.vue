<!-- S01 工程骨架根组件：只验证 Vue/Router/Element Plus 工具链，不含业务页面。 -->
<script setup lang="ts"></script>

<template>
  <el-container class="app-shell">
    <el-header class="app-shell__header">
      <h1 class="app-shell__title">AIDHU 回答判别 Agent</h1>
    </el-header>
    <el-main class="app-shell__main">
      <router-view />
    </el-main>
  </el-container>
</template>
