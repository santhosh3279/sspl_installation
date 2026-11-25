<script setup>
defineProps({ name: String })
const P = {
  home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4z',
  sites: 'M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01',
  license: 'M6 3h9l4 4v14H6zM15 3v4h4M9 12h6M9 16h4',
  backup: 'M4 7c0-2 16-2 16 0v10c0 2-16 2-16 0zM4 7c0 2 16 2 16 0M4 12c0 2 16 2 16 0',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  clock: 'M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 2',
  cloud: 'M7 18a4 4 0 01-.5-8 6 6 0 0111.3 1.5A3.3 3.3 0 0117.5 18z',
  update: 'M20 11a8 8 0 00-14-4L4 9M4 4v5h5M4 13a8 8 0 0014 4l2-2M20 20v-5h-5',
  box: 'M12 3l8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9',
  memory: 'M6 6h12v12H6zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4',
  users: 'M9 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM2.500 20c0-4 3-6 6.500-6s6.500 2 6.500 6M16 4.500a3.500 3.500 0 010 6.500M18 14.500c2.300.8 3.500 2.700 3.500 5.500',
  shield: 'M12 3l8 3v6c0 5-3.500 8-8 9-4.500-1-8-4-8-9V6z',
  list: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  printer: 'M7 9V3h10v6M7 18H4V9h16v9h-3M7 14h10v7H7z',
  tunnel: 'M4 20v-8a8 8 0 0116 0v8M9 20v-7a3 3 0 016 0v7',
  stack: 'M12 3l9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5',
  tool: 'M14.500 6a4 4 0 005 5L11 19.500a2.100 2.100 0 01-3-3z',
  route: 'M6 19a2 2 0 100-4 2 2 0 000 4zM18 9a2 2 0 100-4 2 2 0 000 4zM8 17h6a4 4 0 000-8h-4a0 0 0 010-2h6',
  chat: 'M4 5h16v11H10l-5 4v-4H4z',
  chart: 'M4 20h16M6 16v-5M11 16V5M16 16V9',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'M6 6l12 12M18 6L6 18',
  up: 'M6 15l6-6 6 6',
  down: 'M6 9l6 6 6-6',
}
</script>
<template><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="P[name]" /></svg></template>
