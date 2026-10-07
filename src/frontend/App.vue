<!--
  应用外壳：标题 + 顶部导航 + 路由出口。

  S04-07 起有两条实际路径：判一条（`/`）与批次列表/详情（`/runs`）。
  导航只做跳转，不承载业务状态；页面各自向后端读取真实状态。
-->
<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';

const route = useRoute();

/** 批次详情的地址是 `/runs/{id}`，因此「批次」在详情页也要保持高亮。 */
const activeNav = computed<string>(() => (route.path.startsWith('/runs') ? '/runs' : '/'));
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-shell__header">
      <h1 class="app-shell__title">AIDHU 回答判别 Agent</h1>
      <el-menu :default-active="activeNav" mode="horizontal" class="app-shell__nav" :router="true">
        <el-menu-item index="/">判一条</el-menu-item>
        <el-menu-item index="/runs">批次</el-menu-item>
      </el-menu>
    </el-header>
    <el-main class="app-shell__main">
      <router-view />
    </el-main>
  </el-container>
</template>
