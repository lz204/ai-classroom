<template>
  <div class="app-container">
    <ClassroomView />
  </div>
</template>

<script setup>
import ClassroomView from './views/Classroom.vue'
</script>

<style>
/* 全局基础样式 */
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: #f5f7fa;
}

.app-container {
  min-height: 100vh;
}
</style>
