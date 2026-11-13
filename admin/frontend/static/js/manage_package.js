// static/js/manage_package.js

document.addEventListener('DOMContentLoaded', () => {
  const listEl = document.getElementById('package-list');
  const form = document.getElementById('add-package-form');
  const nameInput = document.getElementById('package_name');
  const submitBtn = form.querySelector('button[type="submit"]');

  const showToast = (message, isError = false) => {
    if (typeof Toastify === 'undefined') { alert(message); return; }
    Toastify({ text: message, duration: 3000, close: true, gravity: 'top', position: 'center', style: { background: isError ? '#DC2626' : '#16A34A' } }).showToast();
  };

  const renderPackages = (packages) => {
    listEl.innerHTML = '';
    if (!packages.length) {
      listEl.innerHTML = '<p class="empty-state">ไม่มีแพ็คเกจ</p>';
      return;
    }
    // Sort alphabetically for consistency
    packages.sort((a, b) => a.id - b.id);
    const table = document.createElement('table');
    table.className = 'data-table';
    table.innerHTML = `
        <thead>
          <tr>
            <th style="width:70px;text-align:left;">ID</th>
            <th style="text-align:left;">Package Name</th>
            <th style="width:90px;">Actions</th>
          </tr>
        </thead>
        <tbody></tbody>`;
    const tbody = table.querySelector('tbody');
    packages.forEach((pkg) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
    <td>${pkg.id}</td>
          <td>${pkg.name}</td>
          <td style="text-align:center;">
            <button class="btn-icon btn-delete" data-id="${pkg.id}" title="ลบแพ็คเกจ">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
            </button>
          </td>`;
      tbody.appendChild(tr);
    });
    listEl.appendChild(table);
  };

  const fetchPackages = async () => {
    listEl.innerHTML = '<p class="loading-state">กำลังโหลด...</p>';
    try {
      const res = await fetch('/ai-camera/api/packages');
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'โหลดแพ็คเกจล้มเหลว');
      renderPackages(data);
    } catch (e) {
      showToast(e.message, true);
      listEl.innerHTML = '<p class="error-state">เกิดข้อผิดพลาด</p>';
    }
  };

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = nameInput.value.trim();
    if (!name) { showToast('กรุณากรอกชื่อแพ็คเกจ', true); return; }
    submitBtn.disabled = true; submitBtn.textContent = 'กำลังบันทึก...';
    try {
      const res = await fetch('/ai-camera/api/packages', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'สร้างแพ็คเกจไม่สำเร็จ');
      showToast('สร้างแพ็คเกจสำเร็จ');
      form.reset();
      fetchPackages();
    } catch (e) { showToast(e.message, true); }
    finally { submitBtn.disabled = false; submitBtn.textContent = 'สร้างแพ็คเกจ'; }
  });

  listEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('.btn-delete');
    if (!btn) return;
    const id = btn.dataset.id;
    if (!confirm('ลบแพ็คเกจนี้?')) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/ai-camera/api/packages/${id}`, { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'ลบแพ็คเกจล้มเหลว');
      showToast('ลบสำเร็จ');
      fetchPackages();
    } catch (e) { showToast(e.message, true); btn.disabled = false; }
  });

  document.getElementById('refresh-packages').addEventListener('click', fetchPackages);
  fetchPackages();
});
