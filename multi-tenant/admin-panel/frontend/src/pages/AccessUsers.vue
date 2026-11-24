<script setup>
import AccessFrame from '../components/AccessFrame.vue'
import UserList from '../components/UserList.vue'
</script>
<template><AccessFrame title="Users" lead="Every site has its own users, passwords and 2FA: the same name on two sites is two accounts. The first login asks for 2FA registration, confirmed by an e-mailed code."><template #default="{ s }"><UserList :s="s" /></template></AccessFrame></template>
