import { createApp, ref, reactive, onMounted, onBeforeUnmount, computed } from 'https://unpkg.com/vue@3/dist/vue.esm-browser.prod.js'

const App = {
  setup(){
    // --- inventory state (existing) ---
    const servers = ref([])
    const selected = ref(null)
    const inventory = ref(null)
    const loading = ref(false)
    const error = ref(null)
    const expandMap = ref({}) // nic.id -> bool

    async function loadServers(){
      try{
        const r = await fetch('/api/servers')
        servers.value = await r.json()
        if(servers.value.length) selected.value = servers.value[0].key
        if(selected.value) await loadInventory(selected.value)
      }catch(e){ error.value = '无法读取服务器列表' }
    }

    async function loadInventory(key){
      loading.value = true
      error.value = null
      try{
        const r = await fetch(`/api/inventory/${key}`)
        const j = await r.json()
        if (j && Array.isArray(j.vms)) {
          j.vms.sort((a, b) => {
            const aHas = Array.isArray(a.nics) && a.nics.length > 0
            const bHas = Array.isArray(b.nics) && b.nics.length > 0
            if (aHas === bHas) {
              try { return (a.name || '').localeCompare(b.name || '') } catch(e) { return 0 }
            }
            return aHas ? -1 : 1
          })
        }
        inventory.value = j
      }catch(e){ error.value = '无法读取区域数据' }
      loading.value = false
    }

    function toggleExpand(nicId){ expandMap.value[nicId] = !expandMap.value[nicId] }

    function primaryIpOf(nic){
      if(!nic || !Array.isArray(nic.ips) || nic.ips.length===0) return null
      for(const ip of nic.ips) if(!ip.includes(':')) return ip
      return nic.ips[0]
    }

    function extraIpsOf(nic){
      if(!nic || !Array.isArray(nic.ips) || nic.ips.length<=1) return []
      const primary = primaryIpOf(nic)
      return nic.ips.filter(ip => ip !== primary)
    }

    function choose(s){ selected.value = s.key; loadInventory(s.key) }

    // modal
    const modal = ref({ show: false, message: '', type: 'info' })
    function showModal(msg, type='info', timeout=2500){
      modal.value.show = true
      modal.value.message = msg
      modal.value.type = type
      if(timeout>0) setTimeout(()=>{ modal.value.show = false }, timeout)
    }

    function getVmPrimaryIp(vm){
      if(!vm || !Array.isArray(vm.nics)) return null
      for(const nic of vm.nics){ const p = primaryIpOf(nic); if(p && !p.includes(':')) return p }
      return null
    }

    async function copySsh(vm){
      const ip = getVmPrimaryIp(vm)
      if(!ip){ showModal('未找到 IPv4 地址，无法生成 SSH 命令', 'error', 4000); return }
      const cmd = `ssh switchpc1@${ip}`
      try{
        if(navigator && navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(cmd)
        else{ const ta = document.createElement('textarea'); ta.value = cmd; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta) }
        showModal(`已复制: ${cmd}`, 'success', 2500)
      }catch(e){ showModal('复制失败，请手动复制: ' + cmd, 'error', 5000) }
    }

    onMounted(()=>{ loadServers() })

    // --- topology editor state ---
    const view = ref('inventory') // 'inventory' or 'topology'
    const topology = reactive({ region: '', nodes: [], links: [] })
  const regions = ref([])
  const createLogs = ref('')
  const creating = ref(false)
  const installInProgress = ref(false)
  const installResults = ref([])
  const installCollapsed = ref(true)
  const swInProgress = ref(false)
  const hostInProgress = ref(false)
  const swResults = ref([])
  const hostResults = ref([])
  const swCollapsed = ref(true)
  const hostCollapsed = ref(true)
  const topologyConfirmed = ref(false)
  const nodeVmMap = reactive({}) // nodeId -> vmName
  const availableVms = ref([]) // list of vm names for current region (filtered)
  const hostIpMap = reactive({}) // host nodeId -> ip string

    async function loadRegions(){
      try{
        const r = await fetch('/api/regions')
        regions.value = await r.json()
        if(regions.value.length && !topology.region) topology.region = regions.value[0]
      }catch(e){ }
    }
    onMounted(()=>{ loadRegions() })
    const topologySel = ref(null) // {type:'node'|'link', id}
    const connectMode = ref(false)
    const connectSrc = ref(null)
    const dragState = ref(null) // { id, offsetX, offsetY }
    const svgRef = ref(null)

    // helpers: generate next id
    function nextId(prefix){
      let i = 1
      const exists = (id)=> topology.nodes.some(n=>n.id===id) || topology.links.some(l=>l.id===id)
      while(true){ const id = `${prefix}${i}`; if(!exists(id)) return id; i++ }
    }

    function addNode(type){
      const id = type==='host' ? nextId('h') : nextId('sw')
      const name = id
      // default position (center of viewport) - if svgRef set, center there
      let x = 300, y = 160
      try{ const bb = svgRef.value && svgRef.value.getBoundingClientRect(); if(bb){ x = Math.round(bb.width/2); y = Math.round(bb.height/2)} }catch(e){}
      topology.nodes.push({ id, type, name, x, y, meta: {} })
      topologySel.value = { type:'node', id }
      topologyConfirmed.value = false
    }

    function clearCanvas(){
      topology.nodes.splice(0, topology.nodes.length)
      topology.links.splice(0, topology.links.length)
      topologySel.value = null
      showModal('画布已清空', 'success')
      topologyConfirmed.value = false
    }

  function findNode(id){ return topology.nodes.find(n=>n.id===id) }
  function findLink(id){ return topology.links.find(l=>l.id===id) }
  const tmpId = ref('')

    function removeSelected(){
      if(!topologySel.value) return
      if(topologySel.value.type==='node'){
        const id = topologySel.value.id
        // remove links referencing node
        topology.links = topology.links.filter(l=>l.a!==id && l.b!==id)
        const idx = topology.nodes.findIndex(n=>n.id===id)
        if(idx>=0) topology.nodes.splice(idx,1)
      }else if(topologySel.value.type==='link'){
        const id = topologySel.value.id
        const idx = topology.links.findIndex(l=>l.id===id)
        if(idx>=0) topology.links.splice(idx,1)
      }
      topologySel.value = null
      topologyConfirmed.value = false
    }

    function startConnect(){ connectMode.value = true; connectSrc.value = null }
    function cancelConnect(){ connectMode.value = false; connectSrc.value = null }

    function nodeClicked(nodeId, evt){
      if(connectMode.value){
        if(!connectSrc.value){ connectSrc.value = nodeId; return }
        const a = connectSrc.value, b = nodeId
        if(a===b){ connectSrc.value = null; return }
        // avoid duplicate identical link (both directions considered same)
        const exists = topology.links.some(l => (l.a===a && l.b===b) || (l.a===b && l.b===a))
        const id = nextId('l')
        const label = `${a}-${b}`
        topology.links.push({ id, a, b, label, meta: {} })
  topologyConfirmed.value = false
        connectSrc.value = null
        connectMode.value = false
        topologySel.value = { type:'link', id }
        return
      }
      // normal select
  topologySel.value = { type:'node', id: nodeId }
  tmpId.value = nodeId
    }

    // drag handling (pointer events)
    function nodePointerDown(evt, node){
      evt.stopPropagation(); evt.preventDefault()
      const svg = svgRef.value
      const pt = { x: evt.clientX, y: evt.clientY }
      dragState.value = { id: node.id, startX: node.x, startY: node.y, sx: pt.x, sy: pt.y }
      window.addEventListener('pointermove', onPointerMove)
      window.addEventListener('pointerup', onPointerUp)
    }

    function onPointerMove(evt){
      if(!dragState.value) return
      const d = dragState.value
      const dx = evt.clientX - d.sx
      const dy = evt.clientY - d.sy
      const n = findNode(d.id)
      if(n){ n.x = d.startX + dx; n.y = d.startY + dy }
    }
    function onPointerUp(){
      dragState.value = null
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
    }

    function canvasClick(){ topologySel.value = null; if(connectMode.value) { connectSrc.value = null } }

    // import/export
    function exportTopology(){
      const out = {
        region: topology.region||'',
        meta: { created_by:'ui', timestamp: Date.now() },
        // include per-node assigned vm name and host IP if present
        nodes: topology.nodes.map(n=>({ id:n.id, type:n.type, name:n.name, x:n.x, y:n.y, meta:n.meta, vm: nodeVmMap[n.id] || '', ip: hostIpMap[n.id] || '' })),
        links: topology.links.map(l=>({ id:l.id, a:l.a, b:l.b, label:l.label, meta:l.meta }))
      }
      const blob = new Blob([JSON.stringify(out, null, 2)], {type:'application/json'})
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = (topology.region?topology.region+'-':'')+'topology.json'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
    }

    // create ports by calling backend API
    async function createPorts(){
      if(!topology.region){ showModal('请选择区域', 'error'); return }
      const linkNames = (adapters && adapters.value && adapters.value.linkNames) ? adapters.value.linkNames : []
      if(!linkNames || linkNames.length===0){ showModal('当前无链路，无需创建端口', 'error'); return }
      creating.value = true
      createLogs.value = ''
      try{
        const payload = { region: topology.region, links: linkNames }
        const r = await fetch('/api/topology/create_ports', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) })
        const j = await r.json()
        if(j.ok){ createLogs.value = (j.logs && (j.logs.stdout || '') ) + '\n' + (j.logs && (j.logs.stderr || '') ) }
        else { createLogs.value = 'ERROR: ' + (j.error || JSON.stringify(j)) + '\n' + (j.trace || '') }
      }catch(e){ createLogs.value = String(e) }
      creating.value = false
    }

    function confirmTopology(){
      // mark topology as confirmed; further edits will clear it
      topologyConfirmed.value = true
      showModal('拓扑已确认，可以创建端口', 'success')
      // load VMs for this region and build default assignments
      loadRegionVmsAndAssignDefaults()
    }

    async function loadRegionVmsAndAssignDefaults(){
      // do not blindly clear nodeVmMap / hostIpMap because imported JSON may have assigned values.
      // Only fill defaults for nodes that do not already have an assignment.
      availableVms.value = []
      // map of vm name -> vm object for quick lookup (includes primary IP)
      const vmInfo = {}
      // expose for template / export
      window.__vmInfo = vmInfo
      if(!topology.region) return
      try{
        const r = await fetch(`/api/inventory/${topology.region}`)
        const j = await r.json()
        const vms = Array.isArray(j.vms) ? j.vms : []
        // filter VMs that have nic info
        const candidates = vms.filter(vm=>Array.isArray(vm.nics) && vm.nics.length>0).map(vm=>{
          // populate vmInfo with primary ip
          vmInfo[vm.name] = { name: vm.name, primaryIp: getVmPrimaryIp(vm) || '' }
          return vm.name
        })
        availableVms.value = candidates

        // derive node lists by type
        const nodes = topology.nodes
        const hostNodes = nodes.filter(n=>n.type==='host').map(n=>n.id)
        const switchNodes = nodes.filter(n=>n.type!=='host').map(n=>n.id)

        // build a set of already assigned VMs (preserve imported assignments)
        const assigned = new Set(Object.values(nodeVmMap).filter(v=>v))

        // default assign only to nodes that don't already have a vm assigned
        let idx = 0
        // advance idx to first candidate that's not already assigned
        while(idx < candidates.length && assigned.has(candidates[idx])) idx++
        for(const nid of hostNodes.concat(switchNodes)){
          if(nodeVmMap[nid] && nodeVmMap[nid].length>0){
            // keep existing assignment (from import or previous selection)
            continue
          }
          // find next unassigned candidate
          while(idx < candidates.length && assigned.has(candidates[idx])) idx++
          if(idx < candidates.length){
            nodeVmMap[nid] = candidates[idx]
            assigned.add(candidates[idx])
            idx++
          }else{
            nodeVmMap[nid] = nodeVmMap[nid] || ''
          }
        }

        // ensure hostIpMap entries exist but do not overwrite imported values
        for(const h of hostNodes){ if(!(h in hostIpMap)) hostIpMap[h] = '' }
      }catch(e){ showModal('加载区域虚拟机失败','error') }
    }

    function vmOptionsFor(nodeId){
      // return available VMs minus those already selected for other nodes, but keep current selection
      const selected = Object.values(nodeVmMap).filter(v=>v)
      return availableVms.value.filter(vm=>{
        if(nodeVmMap[nodeId]===vm) return true
        return !selected.includes(vm)
      })
    }

    function handleVmSelect(nodeId, newVm){
      const oldVm = nodeVmMap[nodeId] || ''
      if(!newVm){ nodeVmMap[nodeId] = ''; return }
      // find other node currently holding newVm
      let otherNode = null
      for(const k of Object.keys(nodeVmMap)){
        if(k!==nodeId && nodeVmMap[k]===newVm){ otherNode = k; break }
      }
      if(otherNode){
        // swap: otherNode gets oldVm
        nodeVmMap[otherNode] = oldVm || ''
      }
      nodeVmMap[nodeId] = newVm
    }

    function vmDisplayFor(nodeId){
      const vmName = nodeVmMap[nodeId]
      if(!vmName) return {name:'-- 未分配 --', ip: ''}
      const info = window.__vmInfo && window.__vmInfo[vmName]
      return { name: vmName, ip: info ? (info.primaryIp || '') : '' }
    }

    function exportNodeVmMapping(){
      // build lines with header and include host IPs
      const lines = []
      const region = topology.region || ''
      lines.push(`Area ${region}`)
      // preserve node order: hosts first then switches
      const hostNodes = topology.nodes.filter(n=>n.type==='host').map(n=>n.id)
      const switchNodes = topology.nodes.filter(n=>n.type!=='host').map(n=>n.id)
      for(const nid of hostNodes){
        const vm = nodeVmMap[nid] || ''
        const ip = hostIpMap[nid] || ''
        lines.push(`${nid} ${vm}${ip? ' ' + ip: ''}`)
      }
      for(const nid of switchNodes){
        const vm = nodeVmMap[nid] || ''
        lines.push(`${nid} ${vm}`)
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = (topology.region?topology.region+'-':'')+'node_vm_mapping.txt'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
    }

    async function installPorts(){
      if(!topology.region){ showModal('请选择区域', 'error'); return }
      // build nodes array and links array from current topology and selections
      const nodes = topology.nodes.map(n=>({ id: n.id, vm: nodeVmMap[n.id] || '', ip: hostIpMap[n.id] || '' }))
      const links = topology.links.map(l=>({ id: l.id, a: l.a, b: l.b, label: l.label || `${l.a}-${l.b}` }))
      installInProgress.value = true
      installResults.value = []
      try{
        const payload = { region: topology.region, nodes, links }
        const r = await fetch('/api/topology/install_ports', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) })
        const j = await r.json()
        if(j.ok){
          installResults.value = j.results || []
          installCollapsed.value = false
          showModal('安装任务已完成，查看下方结果', 'success')
        }else{
          showModal('安装失败: ' + (j.error || JSON.stringify(j)), 'error', 6000)
        }
      }catch(e){ showModal('安装出错: ' + String(e), 'error', 6000) }
      installInProgress.value = false
    }

    // batch configure sw: call backend /api/topology/configure_sw with current topology, nodes, links
    async function batchConfigureSw(){
      if(!topology.region){ showModal('请选择区域', 'error'); return }
      const nodes = topology.nodes.map(n=>({ id: n.id, vm: nodeVmMap[n.id] || '', ip: hostIpMap[n.id] || '', type: n.type }))
      const links = topology.links.map(l=>({ id: l.id, a: l.a, b: l.b, label: l.label || `${l.a}-${l.b}` }))
      swInProgress.value = true
      swResults.value = []
      try{
        const payload = { region: topology.region, nodes, links }
        const r = await fetch('/api/topology/configure_sw', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) })
        const j = await r.json()
        if(j.ok){ swResults.value = j.results || []; swCollapsed.value = false; showModal('批量配置sw完成，查看下方结果', 'success') }
        else { showModal('批量配置sw 失败: ' + (j.error || JSON.stringify(j)), 'error', 6000) }
      }catch(e){ showModal('批量配置sw 出错: ' + String(e), 'error', 6000) }
      swInProgress.value = false
    }

    // batch configure host: call backend /api/topology/configure_host with current topology, nodes, links
    async function batchConfigureHost(){
      if(!topology.region){ showModal('请选择区域', 'error'); return }
      const nodes = topology.nodes.map(n=>({ id: n.id, vm: nodeVmMap[n.id] || '', ip: hostIpMap[n.id] || '', type: n.type }))
      const links = topology.links.map(l=>({ id: l.id, a: l.a, b: l.b, label: l.label || `${l.a}-${l.b}` }))
      hostInProgress.value = true
      hostResults.value = []
      try{
        const payload = { region: topology.region, nodes, links }
        const r = await fetch('/api/topology/configure_host', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) })
        const j = await r.json()
        if(j.ok){ hostResults.value = j.results || []; hostCollapsed.value = false; showModal('批量配置host完成，查看下方结果', 'success') }
        else { showModal('批量配置host 失败: ' + (j.error || JSON.stringify(j)), 'error', 6000) }
      }catch(e){ showModal('批量配置host 出错: ' + String(e), 'error', 6000) }
      hostInProgress.value = false
    }

    async function importTopologyFromText(txt){
      try{
        const obj = JSON.parse(txt)
        // basic validation
        if(!obj.nodes || !obj.links) throw new Error('缺少 nodes 或 links')
        topology.region = obj.region || topology.region
        topology.nodes = obj.nodes.map(n=>({ id:n.id, type:n.type, name:n.name||n.id, x:n.x||100, y:n.y||100, meta:n.meta||{}, vm: n.vm || '', ip: n.ip || '' }))
        topology.links = obj.links.map(l=>({ id:l.id||nextId('l'), a:l.a, b:l.b, label:l.label||`${l.a}-${l.b}`, meta:l.meta||{} }))
        topologySel.value = null

        // ensure VM inventory for this region is loaded before applying imported mappings
        await loadRegionVmsAndAssignDefaults()

        // apply imported vm/ip mapping; use handleVmSelect to preserve uniqueness when possible
        const missing = []
        for(const n of obj.nodes){
          if(!n || !n.id) continue
          const nid = n.id
          if(n.vm){
            // if VM not in available list, still assign but note missing
            if(!availableVms.value.includes(n.vm)) missing.push(n.vm)
            // use swap-safe assign
            try{ handleVmSelect(nid, n.vm) }catch(e){ nodeVmMap[nid] = n.vm }
          }else{
            // clear
            nodeVmMap[nid] = ''
          }
          // host IP
          hostIpMap[nid] = n.ip || ''
        }

        if(missing.length>0){
          const uniq = Array.from(new Set(missing))
          showModal('导入成功，但以下虚拟机在当前区域未发现: ' + uniq.join(', '), 'warning', 6000)
        }else{
          showModal('导入成功', 'success')
        }

        topologyConfirmed.value = false
      }catch(e){ showModal('导入失败: '+String(e), 'error', 4000) }
    }

    function importFromFile(file){
      const r = new FileReader()
      r.onload = ()=> importTopologyFromText(r.result)
      r.readAsText(file)
    }

  // compute adapters: deterministic per-node interface assignment
  const adapters = computed(()=>{
      // build peer lists preserving link order
      const peers = {}
      topology.nodes.forEach(n=>{ peers[n.id] = [] })
      topology.links.forEach(l=>{
        const a = l.a, b = l.b
        if(!peers[a]) peers[a]=[]
        if(!peers[b]) peers[b]=[]
        if(!peers[a].includes(b)) peers[a].push(b)
        if(!peers[b].includes(a)) peers[b].push(a)
      })
      // assign interfaces
      const perNode = {}
      for(const nid of Object.keys(peers)){
        const node = findNode(nid)
        const isHost = node && node.type==='host'
        const list = peers[nid]
        perNode[nid] = []
        let idx = 0
        for(const peer of list){
          const iface = isHost ? `eth${idx}` : `swp${idx+1}`
          perNode[nid].push({ peer, iface })
          idx++
        }
      }
      // link assignments
      const linkAssignments = topology.links.map(l=>{
        const aifs = (perNode[l.a]||[]).find(p=>p.peer===l.b)
        const bifs = (perNode[l.b]||[]).find(p=>p.peer===l.a)
        return { link: l.label||`${l.a}-${l.b}`, a:l.a, a_iface: aifs? aifs.iface : null, b:l.b, b_iface: bifs? bifs.iface : null }
      })
      const linkNames = topology.links.map(l=> l.label||`${l.a}-${l.b}`)
      // also build grouped view by type (hosts / switches) but list link labels per-node
      const grouped = { host: {}, switch: {} }
      // init groups with empty arrays for all nodes
      topology.nodes.forEach(n=>{
        const t = n.type==='host' ? 'host' : 'switch'
        grouped[t][n.id] = []
      })
      // for each link append the link label to both endpoint node's group
      topology.links.forEach(l=>{
        const label = l.label || `${l.a}-${l.b}`
        const na = findNode(l.a)
        const nb = findNode(l.b)
        if(na){ const ta = na.type==='host' ? 'host' : 'switch'; grouped[ta][l.a] = grouped[ta][l.a] || []; if(!grouped[ta][l.a].includes(label)) grouped[ta][l.a].push(label) }
        if(nb){ const tb = nb.type==='host' ? 'host' : 'switch'; grouped[tb][l.b] = grouped[tb][l.b] || []; if(!grouped[tb][l.b].includes(label)) grouped[tb][l.b].push(label) }
      })
      return { perNode, linkAssignments, linkNames, grouped }
    })

    // UI collapse state for grouped adapters panel
    const adaptersCollapsed = reactive({ host: true, switch: true })

    // computed shortcuts for template to avoid calling findNode/findLink repeatedly
    const selectedNode = computed(()=>{
      if(!topologySel.value || topologySel.value.type!=='node') return null
      return findNode(topologySel.value.id)
    })
    const selectedLink = computed(()=>{
      if(!topologySel.value || topologySel.value.type!=='link') return null
      return findLink(topologySel.value.id)
    })

    // keyboard handlers
    // Only perform delete action when focus is NOT inside an input/textarea/select or a contenteditable element.
    function onKeydown(e){
      // ignore when typing in inputs or editable areas
      try{
        const target = e.target || document.activeElement
        const tag = target && target.tagName ? String(target.tagName).toLowerCase() : ''
        const isEditable = target && (target.isContentEditable === true)
        if(tag === 'input' || tag === 'textarea' || tag === 'select' || isEditable) return
      }catch(err){ /* if any issue, fallthrough to normal behavior */ }
      if(e.key==='Delete' || e.key==='Backspace'){ removeSelected() }
    }
    onMounted(()=>{ window.addEventListener('keydown', onKeydown) })
    onBeforeUnmount(()=>{ window.removeEventListener('keydown', onKeydown); window.removeEventListener('pointermove', onPointerMove); window.removeEventListener('pointerup', onPointerUp) })

    // validate id uniqueness when renaming
  function renameNode(node, newId){
      // allow passing a ref from template
      if(newId && typeof newId === 'object' && 'value' in newId) newId = newId.value
      if(!/^[A-Za-z0-9_-]+$/.test(newId)) { showModal('id 只能包含 A-Za-z0-9_-', 'error'); return }
      if(topology.nodes.some(n=>n.id===newId && n!==node) || topology.links.some(l=>l.id===newId)) { showModal('id 已存在', 'error'); return }
      // update links referencing this id
      topology.links.forEach(l=>{ if(l.a===node.id) l.a = newId; if(l.b===node.id) l.b = newId })
      node.id = newId
      tmpId.value = newId
    }

    return {
      // inventory
      servers, selected, inventory, loading, error, choose,
      expandMap, toggleExpand, primaryIpOf, extraIpsOf,
      modal, copySsh,
      // topology
      view, topology, topologySel, connectMode, connectSrc, svgRef,
      addNode, startConnect, cancelConnect, nodeClicked, nodePointerDown, canvasClick,
      exportTopology, importFromFile, importTopologyFromText, removeSelected, renameNode,
  // helpers exposed to template
  findNode, findLink, tmpId, selectedNode, selectedLink,
      // new actions
      clearCanvas, adapters,
      // UI state
      adaptersCollapsed,
      // regions and create ports
      regions, createPorts, createLogs, creating,
      topologyConfirmed, confirmTopology,
  nodeVmMap, vmOptionsFor, exportNodeVmMapping, hostIpMap,
  vmDisplayFor, handleVmSelect,
      installPorts, installInProgress, installResults, installCollapsed,
      batchConfigureSw, batchConfigureHost,
      swInProgress, hostInProgress, swResults, hostResults, swCollapsed, hostCollapsed
    }
  },
  template: `
  <div>
    <div class="header">
      <div class="logo">ES</div>
      <div>
        <div class="title">ESXi拓扑搭建平台</div>
        <div class="small">可视化 ESXi → VM → NIC → IP → 内部网卡名；拖拽式拓扑编辑</div>
      </div>
    </div>

    <div class="grid">
      <div class="sidebar card">
        <div style="font-weight:700;margin-bottom:8px;font-size: 21px;">服务器区域</div>
        <div v-if="servers.length===0" class="empty">无服务器</div>
        <div v-else>
          <div v-for="s in servers" :key="s.key" class="server-item" :class="{active: s.key===selected}" @click="choose(s)">
            <div>
              <div class="server-name">{{s.key}}</div>
              <div class="small">{{s.ip || '无IP'}}</div>
            </div>
            <div class="tag">{{s.in_db? '已入库':'未入库'}}</div>
          </div>
        </div>
        <div style="margin-top:14px;display:flex;flex-direction:column;gap:8px">
          <button class="tool-btn" @click="view='inventory'">清单</button>
          <button class="tool-btn" @click="view='topology'">拓扑编辑器</button>
        </div>
      </div>

      <div class="main-area">
        <!-- Inventory view -->
        <div v-show="view==='inventory'">
          <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <div style="font-weight:700">区域: {{selected}}</div>
              <div class="small">仅显示已发现的内部网卡名称</div>
            </div>
          </div>

          <div v-if="loading" class="card empty">加载中…</div>
          <div v-if="error" class="card empty">{{error}}</div>

          <div v-if="inventory && inventory.vms && inventory.vms.length" class="vm-list">
            <div v-for="vm in inventory.vms" :key="vm.id" class="vm-card">
              <div class="vm-title">
                <div style="display:flex;align-items:center;gap:10px">
                  <img class="vm-logo" :src="vm.nics && vm.nics.length ? '/computer_yes.png' : '/computer_no.png'" :alt="vm.name" @click.prevent="copySsh(vm)" title="点击复制 SSH 命令">
                  <div><strong>{{vm.name}}</strong></div>
                </div>
                <div class="small">{{vm.nics.length}} 网卡</div>
              </div>
              <div v-if="vm.nics.length===0" class="small">无网卡信息</div>
              <div v-for="nic in vm.nics" :key="nic.id" class="nic">
                <div style="display:flex;justify-content:space-between;align-items:center">
                  <div>
                    <div><strong>{{nic.name}}</strong> <span class="small">{{nic.mac || '-'}}</span></div>
                    <div class="small">IP:
                      <span v-if="nic.ips.length">
                        <span>{{ primaryIpOf(nic) || '-' }}</span>
                        <template v-if="extraIpsOf(nic).length">
                          <button class="expand-btn" @click="toggleExpand(nic.id)">{{ expandMap[nic.id] ? '收起' : '展开' }}</button>
                        </template>
                      </span>
                      <span v-else>-</span>
                    </div>
                    <div v-if="expandMap[nic.id] && extraIpsOf(nic).length" class="ip-list small">
                      <div v-for="ip in extraIpsOf(nic)" :key="ip">{{ip}}</div>
                    </div>
                  </div>
                  <div style="text-align:right">
                    <div class="tag" style="background:transparent;color:var(--accent)">{{nic.inner_name || '-'}}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-else-if="inventory && inventory.vms && inventory.vms.length===0" class="card empty">该区域没有 VM 数据</div>
          <div class="footer">数据来自本地 SQLite 数据库（esxi_data.db）。</div>
        </div>

        <!-- Topology editor view -->
        <div v-show="view==='topology'" class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <div style="font-weight:700">拓扑画布（区域: <span class="topology-region"><select v-model="topology.region">
              <option v-for="r in regions" :key="r" :value="r">{{r}}</option>
            </select></span>）</div>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="file" @change="e=>importFromFile(e.target.files[0])" />
              <button @click="exportTopology">导出 JSON</button>
            </div>
          </div>

          <div class="topology-shell">
            <div class="topology-tools">
              <button @click.prevent="addNode('host')">新建 Host</button>
              <button @click.prevent="addNode('switch')">新建 Switch</button>
              <button @click.prevent="startConnect">连线</button>
              <button @click.prevent="cancelConnect">取消连线</button>
              <button @click.prevent="removeSelected">删除选中</button>
              <button class="clear-btn" @click.prevent="clearCanvas">清空画布</button>
            </div>

            <div class="topology-canvas" @click="canvasClick">
              <svg ref="svgRef" style="width:100%;height:520px;background:linear-gradient(90deg, rgba(255,255,255,0.01), rgba(0,0,0,0.04));" @pointerdown.stop>
                <!-- links -->
                <g>
                  <line v-for="l in topology.links" :key="l.id" :x1="findNode(l.a).x" :y1="findNode(l.a).y" :x2="findNode(l.b).x" :y2="findNode(l.b).y" stroke="#6ee7b7" stroke-width="2" :class="{'link-selected': topologySel && topologySel.type==='link' && topologySel.id===l.id}" @click.stop="topologySel={type:'link',id:l.id}" />
                </g>
                <!-- nodes -->
                <g>
                  <g v-for="n in topology.nodes" :key="n.id" :transform="'translate(' + (n.x-40) + ',' + (n.y-20) + ')'" class="node" @pointerdown.prevent="nodePointerDown($event,n)" @click.stop="nodeClicked(n.id,$event)">
                    <rect :width="80" :height="40" rx="8" :fill="n.type==='host'? 'rgba(96,165,250,0.12)' : 'rgba(110,231,183,0.06)'" stroke="rgba(255,255,255,0.06)" />
                    <text x="40" y="22" fill="#e6eef8" font-size="12" text-anchor="middle" alignment-baseline="middle">{{n.name}}</text>
                  </g>
                </g>
              </svg>
            </div>

            <div class="topology-panel card">
              <div v-if="topologySel && topologySel.type==='node'">
                <div style="font-weight:700">节点属性</div>
                <div class="small">id: {{ selectedNode && selectedNode.id }}</div>
                <div style="margin-top:8px">名称: <input v-model="selectedNode.name" /></div>
                <div style="margin-top:8px">类型: {{ selectedNode.type }}</div>
                <div style="margin-top:8px">位置: X <input style="width:60px" v-model.number="selectedNode.x" /> Y <input style="width:60px" v-model.number="selectedNode.y" /></div>
                <div style="margin-top:8px">重命名 id: <input v-model="tmpId" placeholder="新 id" @keydown.enter.prevent="renameNode(selectedNode, tmpId)" /></div>
              </div>
              <div v-else-if="topologySel && topologySel.type==='link'">
                <div style="font-weight:700">链路属性</div>
                <div class="small">id: {{ selectedLink && selectedLink.id }}</div>
                <div style="margin-top:8px">label: <input v-model="selectedLink.label" /></div>
                <div style="margin-top:8px">A: {{ selectedLink.a }} &nbsp; B: {{ selectedLink.b }}</div>
              </div>
              <div v-else>
                <div style="font-weight:700">提示</div>
                <div class="small">选择节点或链路以编辑属性。使用左侧工具进行创建与连线。</div>
                <div class="adapters">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                    <div style="font-weight:600">将创建的网络适配器</div>
                    <div class="small">仅显示简洁链接列表；展开可查看按机器分组</div>
                  </div>
                  <div v-if="adapters.linkNames.length===0" class="small">无（当前无链路）</div>
                  <div v-else>
                    <!-- compact link-only list -->
                    <div class="small">需要创建的端口组：</div>
                    <ul class="compact-links">
                      <li v-for="name in adapters.linkNames" :key="name">{{name}}</li>
                    </ul>

                    <!-- collapsible grouped-by-node view -->
                    <div style="margin-top:8px">
                      <button class="tool-btn" @click.prevent="adaptersCollapsed.host = !adaptersCollapsed.host">{{ adaptersCollapsed.host ? '展开' : '折叠' }} Hosts 列表</button>
                      <div v-show="!adaptersCollapsed.host" class="grouped-list small">
                        <div v-for="(ifs, nid) in adapters.grouped.host" :key="nid" style="margin-top:6px">
                          <div style="font-weight:600">{{nid}}</div>
                          <div>{{ ifs.join(', ') || '-' }}</div>
                        </div>
                      </div>

                      <button class="tool-btn" style="margin-top:8px" @click.prevent="adaptersCollapsed.switch = !adaptersCollapsed.switch">{{ adaptersCollapsed.switch ? '展开' : '折叠' }} Switches 列表</button>
                      <div v-show="!adaptersCollapsed.switch" class="grouped-list small">
                        <div v-for="(ifs, nid) in adapters.grouped.switch" :key="nid" style="margin-top:6px">
                          <div style="font-weight:600">{{nid}}</div>
                          <div>{{ ifs.join(', ') || '-' }}</div>
                        </div>
                      </div>
                      <!-- create ports button -->
                      <div style="margin-top:10px">
                        <button v-if="!topologyConfirmed" class="tool-btn" @click.prevent="confirmTopology"  style="color: #64b7e7;">确认拓扑</button>
                        <button v-else class="tool-btn" @click.prevent="createPorts" :disabled="creating" style="color: #64b7e7;">{{ creating ? '正在创建...' : '1️⃣ESXI创建端口' }}</button>
                      </div>
                      <div style="margin-top:8px">
                        <div style="font-weight:600">创建结果</div>
                        <pre class="logs" v-if="createLogs">{{ createLogs }}</pre>
                      </div>

                      <!-- persistent batch buttons: always visible even if topologyConfirmed is false -->
                      <div style="display:flex;gap:8px;margin-top:8px">
                        <button class="tool-btn" @click.prevent="batchConfigureSw" :disabled="swInProgress">{{ swInProgress ? '批量配置中...' : '批量配置sw' }}</button>
                        <button class="tool-btn" @click.prevent="batchConfigureHost" :disabled="hostInProgress">批量配置host</button>
                      </div>

                      <!-- SW results panel -->
                      <div style="margin-top:8px">
                        <button class="tool-btn" @click.prevent="swCollapsed = !swCollapsed">{{ swCollapsed ? '展开 SW 结果' : '折叠 SW 结果' }}</button>
                        <div v-show="!swCollapsed" style="margin-top:8px">
                          <div v-for="res in swResults" :key="res.cmd" class="card" style="margin-bottom:8px;padding:8px">
                            <div style="font-weight:700">{{ res.cmd }}</div>
                            <details>
                              <summary>输出 (展开/收起)</summary>
                              <pre class="logs">STDOUT:\n{{ res.stdout }}\nSTDERR:\n{{ res.stderr }}</pre>
                            </details>
                          </div>
                        </div>
                      </div>

                      <!-- Host results panel -->
                      <div style="margin-top:8px">
                        <button class="tool-btn" @click.prevent="hostCollapsed = !hostCollapsed">{{ hostCollapsed ? '展开 Host 结果' : '折叠 Host 结果' }}</button>
                        <div v-show="!hostCollapsed" style="margin-top:8px">
                          <div v-for="res in hostResults" :key="res.cmd" class="card" style="margin-bottom:8px;padding:8px">
                            <div style="font-weight:700">{{ res.cmd }}</div>
                            <details>
                              <summary>输出 (展开/收起)</summary>
                              <pre class="logs">STDOUT:\n{{ res.stdout }}\nSTDERR:\n{{ res.stderr }}</pre>
                            </details>
                          </div>
                        </div>
                      </div>

                      <!-- machine selector: only show after topology confirmed -->
                      <div v-if="topologyConfirmed" class="machine-selector">
                        <div style="font-weight:600;margin-bottom:6px">机器选择（为代号分配实际虚拟机，选择不能重复）</div>
                        <div class="selector-grid">
                          <div class="hosts-column">
                            <div class="selector-col-title">Hosts</div>
                            <div v-for="n in topology.nodes.filter(x=>x.type==='host')" :key="n.id" class="host-item">
                              <div class="host-id">{{n.id}}</div>
                              <div class="host-info">
                                <div style="display:flex;gap:8px;align-items:center">
                                  <select :value="nodeVmMap[n.id]" @change="e=>handleVmSelect(n.id, e.target.value)">
                                    <option value="">-- 未分配 --</option>
                                    <option v-for="opt in vmOptionsFor(n.id)" :key="opt" :value="opt">{{opt}}</option>
                                  </select>
                                  <input class="host-ip" v-model="hostIpMap[n.id]" placeholder="host IP (可选)" />
                                </div>
                                <div style="margin-top:4px">
                                  <div>{{ vmDisplayFor(n.id).name }}</div>
                                  <div class="small ip">{{ vmDisplayFor(n.id).ip }}</div>
                                </div>
                              </div>
                            </div>
                          </div>
                          <div class="switches-column">
                            <div class="selector-col-title">Switches</div>
                            <div class="switch-grid">
                              <div v-for="n in topology.nodes.filter(x=>x.type!=='host')" :key="n.id" class="switch-item">
                                <div class="switch-id">{{n.id}}</div>
                                <div style="width:100%">
                                  <select :value="nodeVmMap[n.id]" @change="e=>handleVmSelect(n.id, e.target.value)" style="width:100%">
                                    <option value="">-- 未分配 --</option>
                                    <option v-for="opt in vmOptionsFor(n.id)" :key="opt" :value="opt">{{opt}}</option>
                                  </select>
                                </div>
                                <div class="switch-vm">{{ vmDisplayFor(n.id).name }}</div>
                                <div class="small ip">{{ vmDisplayFor(n.id).ip }}</div>
                              </div>
                            </div>
                          </div>
                        </div>
                        <div style="display:flex;justify-content:flex-end;margin-top:8px;flex-direction:column;gap:8px">
                          <div style="display:flex;justify-content:flex-end;gap:8px">
                            <button class="tool-btn" @click.prevent="exportNodeVmMapping">导出虚拟机选择</button>
                            <button class="tool-btn" @click.prevent="installPorts" :disabled="installInProgress"  style="color: #64b7e7;">{{ installInProgress ? '安装中...' : '2️⃣VM安装端口' }}</button>
                          </div>
                          <div>
                            <button class="tool-btn" style="width:100%" @click.prevent="installCollapsed = !installCollapsed">{{ installCollapsed ? '展开安装结果' : '折叠安装结果' }}</button>
                            <div v-show="!installCollapsed" style="margin-top:8px">
                              <div v-for="res in installResults" :key="res.cmd" class="card" style="margin-bottom:8px;padding:8px">
                                <div style="font-weight:700">{{ res.cmd }}</div>
                                <details>
                                  <summary>输出 (展开/收起)</summary>
                                  <pre class="logs">STDOUT:\n{{ res.stdout }}\nSTDERR:\n{{ res.stderr }}</pre>
                                </details>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div :class="['ui-modal', modal.show ? 'show' : '', modal.type]" v-if="modal.show">
      <div class="msg">{{ modal.message }}</div>
    </div>
  </div>
  `
}

createApp(App).mount('#app')
