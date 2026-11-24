<script setup>defineProps({ title: String, lead: String, back: String, backLabel: String })</script>
<template>
  <header class="page-head">
    <div>
      <RouterLink v-if="back" class="back" :to="back">Back to {{ backLabel }}</RouterLink>
      <h1>{{ title }}</h1>
      <p v-if="lead || $slots.lead"><slot name="lead">{{ lead }}</slot></p>
    </div>
    <div v-if="$slots.default" class="actions"><slot /></div>
  </header>
</template>
