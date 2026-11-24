// Every call to the Flask API. A 401 means the session ended: go and log in.
export async function api(url, opts) {
  const r = await fetch(url, opts)
  if (r.status === 401) {
    location.href = '/login'
    return new Promise(() => {})       // the page is going away: never settle
  }
  return r
}

export async function getJSON(url) {
  const r = await api(url)
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  return r.json()
}

export async function postJSON(url, body) {
  const r = await api(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  return r.json()
}
