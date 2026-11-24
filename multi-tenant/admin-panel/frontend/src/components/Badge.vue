<script setup>defineProps({ kind: { type: String, default: '' } })</script>
<template><span class="badge" :class="kind"><slot /></span></template>
