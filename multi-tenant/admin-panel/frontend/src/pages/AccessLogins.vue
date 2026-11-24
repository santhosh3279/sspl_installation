<script setup>
import AccessFrame from '../components/AccessFrame.vue'
import LoginLog from '../components/LoginLog.vue'
</script>
<template><AccessFrame title="Login history" lead="From the site's Authelia login log. A remembered browser does not enter 2FA again, so it is not listed each time."><template #default="{ s }"><LoginLog :site="s.name" /></template></AccessFrame></template>
