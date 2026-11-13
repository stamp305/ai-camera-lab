// static/js/script.js

document.addEventListener('DOMContentLoaded', () => {
    // --- Global State ---
    let allGroupsData = [];
    let selectedRows = [];

    // --- DOM Elements ---
    const accordionContainer = document.getElementById('accordion-container');
    const groupCountElement = document.getElementById('group-count');
    const updateAllButton = document.getElementById('update-all-button');
    const loadingSkeleton = document.getElementById('loading-skeleton');
    const actionBar = document.getElementById('action-bar');
    const updateSelectedButton = document.getElementById('update-selected-button');
    const selectionCounter = document.getElementById('selection-counter');
    const searchInput = document.getElementById('search-input');

    // --- Toast Notification Helper ---
    const showToast = (message, isError = false) => {
        Toastify({
            text: message,
            duration: 3000,
            close: true,
            gravity: "top",
            position: "center",
            style: {
                background: isError ? "#DC2626" : "#16A34A",
            },
        }).showToast();
    };

    // --- UI Update Functions ---
    const updateActionBar = () => {
        const count = selectedRows.length;
        if (count > 0) {
            selectionCounter.textContent = `เลือกทั้งหมด ${count} รายการ`;
            updateSelectedButton.disabled = false;
            actionBar.classList.add('visible');
        } else {
            selectionCounter.textContent = `เลือก 0 รายการ`;
            updateSelectedButton.disabled = true;
            actionBar.classList.remove('visible');
        }
    };

    // --- API Call Handlers ---
    const handleUpdateAll = async () => {
        if (!confirm("คุณแน่ใจหรือไม่ว่าต้องการบังคับอัพเดตเวอร์ชันทุกสาขา?\n\nคำสั่งนี้จะทำการส่งไฟล์อัพเดตไปยังเครื่องสาขาทั้งหมด และบังคับเครื่องสาขารีสตาร์ททันที!")) {
            return;
        }
        try {
            const response = await fetch('/ai-camera/api/update-version-all', { method: 'POST' });
            if (response.ok) {
                showToast(`ส่งคำสั่งอัพเดตไปยังทุกสาขาเรียบร้อยแล้ว`);
            } else {
                throw new Error("Update failed");
            }
        } catch (error) {
            console.error("Update all error:", error);
            showToast("เกิดข้อผิดพลาดในการอัพเดตข้อมูล", true);
        }
    };

    const handleUpdateSelected = async () => {
        if (selectedRows.length === 0) return;
        if (!confirm(`คุณแน่ใจหรือไม่ว่าต้องการบังคับอัพเดตเวอร์ชันสำหรับ ${selectedRows.length} สาขาที่เลือก?\n\nคำสั่งนี้จะทำการส่งไฟล์อัพเดตและบังคับเครื่องสาขารีสตาร์ททันที!`)) {
            return;
        }
        try {
            const response = await fetch('/ai-camera/api/update-version', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ activation_code_list: selectedRows }),
            });
            if (response.ok) {
                showToast(`ส่งคำสั่งอัพเดต ${selectedRows.length} รายการสำเร็จ`);
                selectedRows = []; // Clear selection
                // Re-render open tables to reflect cleared selection
                document.querySelectorAll('details[open]').forEach(details => {
                    const contentDiv = details.querySelector('.content');
                    if (contentDiv) contentDiv.innerHTML = ''; // Clear
                    details.dispatchEvent(new Event('toggle')); // Re-trigger toggle to re-render
                });
                updateActionBar(); // Hide the bar
            } else {
                throw new Error("Update failed");
            }
        } catch (error) {
            console.error("Update selected error:", error);
            showToast("อัพเดตไม่สําเร็จ", true);
        }
    };

    // --- NEW: API Call Handler for Deleting a user ---
    const handleDeleteUser = async (activationCode) => {
        try {
            const response = await fetch(`/ai-camera/api/user/${activationCode}`, {
                method: 'DELETE'
            });
            if (response.ok) {
                showToast(`ลบผู้ใช้ ${activationCode} สำเร็จแล้ว`);
                // Reset selection in case the deleted item was selected
                selectedRows = selectedRows.filter(code => code !== activationCode);
                updateActionBar();
                // Refetch all data to update the UI completely
                fetchData();
            } else {
                const errorData = await response.json();
                throw new Error(errorData.error || "Delete failed");
            }
        } catch (error) {
            console.error("Delete user error:", error);
            showToast(`เกิดข้อผิดพลาดในการลบผู้ใช้: ${error.message}`, true);
        }
    };

    // --- Main Data Fetching and Rendering ---
    const fetchData = async () => {
        // Show skeleton on refetch
        loadingSkeleton.style.display = 'block';
        accordionContainer.innerHTML = '';
        try {
            const response = await fetch('/ai-camera/api/users');
            if (!response.ok) throw new Error('Network response was not ok');
            allGroupsData = await response.json();
            // Expose group names globally for external UI (group selector)
            window.__allGroupNames = Array.isArray(allGroupsData) ? allGroupsData.map(g => g.group_name) : [];
            // Collect unique versions
            const versionSet = new Set();
            allGroupsData.forEach(g => (g.users || []).forEach(u => { if (u.version !== undefined && u.version !== null) versionSet.add(String(u.version)); }));
            window.__allGroupVersions = Array.from(versionSet).sort((a, b) => a.localeCompare(b, 'th', { numeric: true }));
            window.dispatchEvent(new CustomEvent('users:groups-ready', { detail: { groups: window.__allGroupNames, versions: window.__allGroupVersions } }));

            loadingSkeleton.style.display = 'none';
            // Use the current search term to re-filter if it exists
            handleSearch();
        } catch (error) {
            console.error("Error fetching data:", error);
            loadingSkeleton.innerHTML = `<p class="text-red-500">Failed to load user data.</p>`;
            showToast('เกิดข้อผิดพลาดในการโหลดข้อมูล', true);
        }
    };

    const renderAccordions = (groups, openAll = false) => {
        const selectedSet = window.__groupFilterSelected instanceof Set ? window.__groupFilterSelected : null;
        let visibleGroups = groups;
        if (selectedSet && selectedSet.size > 0) {
            visibleGroups = groups.filter(g => selectedSet.has(g.group_name));
        } else if (selectedSet && selectedSet.size === 0) {
            // Explicitly no selection chosen yet: show placeholder instead of all groups
            accordionContainer.innerHTML = `<div class="empty-placeholder" style="padding:60px 0; text-align:center; color:var(--text-secondary); font-size:14px;">เลือกกลุ่มทางด้านบนเพื่อแสดงรายการผู้ใช้</div>`;
            groupCountElement.textContent = 'ยังไม่เลือกกลุ่ม';
            return;
        }

        groupCountElement.textContent = `จำนวนกลุ่มผู้ใช้: ${visibleGroups.length}`;
        accordionContainer.innerHTML = '';

        if (visibleGroups.length === 0) {
            accordionContainer.innerHTML = `<p style="color: var(--text-secondary); text-align: center; padding: 40px 0;">ไม่พบข้อมูลที่ตรงกับการค้นหา</p>`;
            return;
        }

        visibleGroups.forEach((group, index) => {
            const details = document.createElement('details');
            details.className = 'accordion-item';
            if (openAll || (index === 0 && searchInput.value.trim() === '')) {
                details.open = true;
            }

            details.innerHTML = `
                <summary>
                    <div class="accordion-header">
                        <span>${group.group_name} (${group.users.length})</span>
                        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                    </div>
                </summary>
                <div class="content"></div>
            `;
            accordionContainer.appendChild(details);

            details.addEventListener('toggle', (event) => {
                if (event.target.open) {
                    const contentDiv = details.querySelector('.content');
                    createUserTable(contentDiv, group.users, group.group_name);
                }
            });
            if (details.open) {
                details.dispatchEvent(new Event('toggle'));
            }
        });
    };

    const debounce = (func, delay) => {
        let timeoutId;
        return (...args) => {
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => {
                func.apply(this, args);
            }, delay);
        };
    };

    const handleSearch = () => {
        const searchTerm = searchInput.value.trim().toLowerCase();

        // Build filter sets (null = no constraint)
        const statusSet = (window.__statusFilterSelected instanceof Set && window.__statusFilterSelected.size > 0) ? window.__statusFilterSelected : null;
        const versionSet = (window.__versionFilterSelected instanceof Set && window.__versionFilterSelected.size > 0) ? window.__versionFilterSelected : null;
        const activatedSet = (window.__activatedFilterSelected instanceof Set && window.__activatedFilterSelected.size > 0) ? window.__activatedFilterSelected : null;

        const textFilterActive = searchTerm !== '';

        const filteredGroups = allGroupsData.map(group => {
            let users = group.users;
            if (textFilterActive) {
                users = users.filter(user =>
                    (user.activation_code && user.activation_code.toLowerCase().includes(searchTerm)) ||
                    (user.serial_number && user.serial_number.toLowerCase().includes(searchTerm)) ||
                    (user.branch_name && user.branch_name.toLowerCase().includes(searchTerm))
                );
            }
            if (statusSet) {
                users = users.filter(u => statusSet.has(u.is_online ? 'online' : 'offline'));
            }
            if (versionSet) {
                users = users.filter(u => versionSet.has(String(u.version)));
            }
            if (activatedSet) {
                users = users.filter(u => activatedSet.has(u.is_activated ? 'yes' : 'no'));
            }
            return { ...group, users };
        }).filter(group => group.users.length > 0);

        renderAccordions(filteredGroups, true);
    };

    const createUserTable = (container, users, groupName) => {
        let currentPage = 1;
        const itemsPerPage = 5;
        const totalPages = Math.ceil(users.length / itemsPerPage);

        const renderPage = () => {
            container.innerHTML = ''; // Clear container for re-render
            const startIndex = (currentPage - 1) * itemsPerPage;
            const paginatedData = users.slice(startIndex, startIndex + itemsPerPage);

            const allOnPageAreSelected = paginatedData.length > 0 && paginatedData.every(item => selectedRows.includes(item.activation_code));

            const card = document.createElement('div');
            card.className = 'card';
            card.innerHTML = `
                <div class="card-content">
                    <div class="table-container" style="overflow: visible;">
                        <table>
                            <thead>
                                <tr>
                                    <th><input type="checkbox" class="select-all-checkbox" ${allOnPageAreSelected ? 'checked' : ''}></th>
                                    <th>Activation Code</th>
                                    <th>หมายเลขเครื่อง</th>
                                    <th>สาขา</th>
                                    <th>สถานะ</th>
                                    <th>เวอร์ชัน</th>
                                    <th>เปิดใช้งานแล้ว</th>
                                    <th>การกระทำ</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${paginatedData.map(item => `
                                    <tr>
                                        <td><input type="checkbox" class="row-checkbox" data-code="${item.activation_code}" ${selectedRows.includes(item.activation_code) ? 'checked' : ''}></td>
                                        <td>${item.activation_code}</td>
                                        <td>${item.serial_number}</td>
                                        <td>${item.branch_name || '-'}</td>
                                        <td><span class="status-badge status-${item.is_online ? 'online' : 'offline'}">${item.is_online ? 'ออนไลน์' : 'ออฟไลน์'}</span></td>
                                        <td>${item.version || '-'}</td>
                                        <td><span class="status-badge status-${item.is_activated ? 'yes' : 'no'}">${item.is_activated ? 'ใช่' : 'ไม่'}</span></td>
                                        <td class="action-cell">
                                            <div class="dropdown-container" style="position:relative; display:inline-block;">
                                                <button class="btn btn-secondary dropdown-toggle-btn" style="padding:4px 8px;">
                                                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                                        <circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/>
                                                    </svg>
                                                </button>
                                                <div class="dropdown-menu hidden" style="position:absolute; right:0; top:100%; z-index:50; background:#fff; border:1px solid #e5e7eb; border-radius:6px; box-shadow:0 4px 6px rgba(0,0,0,.1); width:160px; text-align:left;">
                                                    <a href="/ai-camera/manage_dlc/${item.activation_code}" style="display:block; padding:8px 12px; color:#374151; text-decoration:none; font-size:13px; border-bottom:1px solid #f3f4f6;">ตั้งค่าบริการ</a>
                                                    <button class="move-group-btn" data-code="${item.activation_code}" data-group="${groupName}" data-branch="${item.branch_name || ''}" style="display:block; width:100%; text-align:left; padding:8px 12px; background:none; border:none; border-bottom:1px solid #f3f4f6; color:#374151; cursor:pointer; font-size:13px;">ย้ายกลุ่ม</button>
                                                    <button class="edit-branch-btn" data-code="${item.activation_code}" data-group="${groupName}" data-branch="${item.branch_name || ''}" style="display:block; width:100%; text-align:left; padding:8px 12px; background:none; border:none; border-bottom:1px solid #f3f4f6; color:#374151; cursor:pointer; font-size:13px;">แก้ไขชื่อสาขา</button>
                                                    <button class="delete-btn" data-code="${item.activation_code}" style="display:block; width:100%; text-align:left; padding:8px 12px; background:none; border:none; color:#dc2626; cursor:pointer; font-size:13px;">ลบ</button>
                                                </div>
                                            </div>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                    <!-- Pagination Controls -->
                    <div class="pagination-controls" style="text-align: center; padding: 16px 0 8px;"></div>
                </div>
            `;
            container.appendChild(card);

            if (totalPages > 1) {
                const paginationContainer = card.querySelector('.pagination-controls');
                const prevButton = document.createElement('button');
                prevButton.textContent = 'Previous';
                prevButton.className = 'btn';
                prevButton.style.marginRight = '8px';
                prevButton.disabled = currentPage === 1;
                prevButton.onclick = () => { currentPage--; renderPage(); };

                const pageIndicator = document.createElement('span');
                pageIndicator.textContent = `Page ${currentPage} of ${totalPages}`;
                pageIndicator.style.color = '#6B7280';

                const nextButton = document.createElement('button');
                nextButton.textContent = 'Next';
                nextButton.className = 'btn';
                nextButton.style.marginLeft = '8px';
                nextButton.disabled = currentPage === totalPages;
                nextButton.onclick = () => { currentPage++; renderPage(); };

                paginationContainer.append(prevButton, pageIndicator, nextButton);
            }

            // --- Attach Event Listeners for this table instance ---
            card.querySelector('.select-all-checkbox').addEventListener('change', (e) => {
                const pageCodes = paginatedData.map(item => item.activation_code);
                if (e.target.checked) {
                    selectedRows = [...new Set([...selectedRows, ...pageCodes])];
                } else {
                    selectedRows = selectedRows.filter(code => !pageCodes.includes(code));
                }
                updateActionBar();
                renderPage();
            });

            card.querySelectorAll('.row-checkbox').forEach(checkbox => {
                checkbox.addEventListener('change', (e) => {
                    const code = e.target.dataset.code;
                    if (e.target.checked) {
                        if (!selectedRows.includes(code)) selectedRows.push(code);
                    } else {
                        selectedRows = selectedRows.filter(c => c !== code);
                    }
                    updateActionBar();
                    card.querySelector('.select-all-checkbox').checked = paginatedData.every(item => selectedRows.includes(item.activation_code));
                });
            });
            
            // --- Dropdown Toggle Logic ---
            card.querySelectorAll('.dropdown-toggle-btn').forEach(button => {
                button.addEventListener('click', (e) => {
                    e.stopPropagation();
                    // Close all other dropdowns first
                    document.querySelectorAll('.dropdown-menu').forEach(menu => menu.classList.add('hidden'));
                    // Toggle current dropdown
                    const menu = e.currentTarget.nextElementSibling;
                    menu.classList.toggle('hidden');
                });
            });

            // --- Attach Event Listeners for Actions ---
            card.querySelectorAll('.delete-btn').forEach(button => {
                button.addEventListener('click', (e) => {
                    const code = e.target.dataset.code;
                    if (confirm(`คุณแน่ใจหรือไม่ว่าต้องการลบผู้ใช้ ${code}?`)) {
                        handleDeleteUser(code);
                    }
                });
            });

            card.querySelectorAll('.move-group-btn').forEach(button => {
                button.addEventListener('click', (e) => {
                    const code = e.target.dataset.code;
                    const group = e.target.dataset.group;
                    const branch = e.target.dataset.branch;
                    openMoveGroupModal(code, group, branch);
                });
            });
            
            card.querySelectorAll('.edit-branch-btn').forEach(button => {
                button.addEventListener('click', (e) => {
                    const code = e.target.dataset.code;
                    const group = e.target.dataset.group;
                    const branch = e.target.dataset.branch;
                    openEditBranchModal(code, group, branch);
                });
            });
        };
        renderPage();
    };
    
    // Close dropdowns when clicking outside
    document.addEventListener('click', () => {
        document.querySelectorAll('.dropdown-menu').forEach(menu => {
            menu.classList.add('hidden');
        });
    });

    // --- Move Group Modal Logic ---
    let currentMoveGroupCode = null;
    let currentMoveGroupBranch = null; // Store current branch so we don't overwrite with null
    const moveGroupModal = document.getElementById('move-group-modal');
    
    const openMoveGroupModal = (code, currentGroup, currentBranch) => {
        currentMoveGroupCode = code;
        currentMoveGroupBranch = currentBranch;
        document.getElementById('move-group-activation-code').textContent = code;
        const select = document.getElementById('move-group-select');
        const newInput = document.getElementById('move-group-new');
        
        newInput.value = '';
        select.innerHTML = '<option value="">-- เลือกกลุ่มที่มีอยู่ --</option>';
        window.__allGroupNames.forEach(name => {
            select.innerHTML += `<option value="${name}" ${name === currentGroup ? 'selected' : ''}>${name}</option>`;
        });
        
        // Reset toggle to 'existing'
        const existingRadio = document.querySelector('input[name="move-group-mode"][value="existing"]');
        if (existingRadio) existingRadio.checked = true;
        document.getElementById('move-group-select-container').style.display = 'block';
        document.getElementById('move-group-new-container').style.display = 'none';
        
        moveGroupModal.style.display = 'flex';
        moveGroupModal.classList.remove('hidden');
    };

    document.getElementById('move-group-cancel')?.addEventListener('click', () => {
        moveGroupModal.style.display = 'none';
        moveGroupModal.classList.add('hidden');
    });

    document.getElementById('move-group-save')?.addEventListener('click', async () => {
        const select = document.getElementById('move-group-select');
        const newInput = document.getElementById('move-group-new');
        
        const modeInput = document.querySelector('input[name="move-group-mode"]:checked');
        const targetGroup = modeInput && modeInput.value === 'new' ? newInput.value.trim() : select.value;
        
        if (!targetGroup) {
            showToast('กรุณาเลือกหรือพิมพ์ชื่อกลุ่มปลายทาง', true);
            return;
        }
        
        try {
            const response = await fetch(`/ai-camera/api/user/${currentMoveGroupCode}/info`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ group_name: targetGroup, branch_name: currentMoveGroupBranch || null })
            });
            if (response.ok) {
                showToast(`ย้ายกลุ่มสำเร็จแล้ว`);
                moveGroupModal.style.display = 'none';
                moveGroupModal.classList.add('hidden');
                fetchData(); // reload
            } else {
                throw new Error('Failed to move group');
            }
        } catch (err) {
            showToast(`เกิดข้อผิดพลาดในการย้ายกลุ่ม`, true);
        }
    });
    
    // --- Edit Branch Modal Logic ---
    let currentEditBranchCode = null;
    let currentEditBranchGroup = null; // Store current group
    const editBranchModal = document.getElementById('edit-branch-modal');
    
    const openEditBranchModal = (code, currentGroup, currentBranch) => {
        currentEditBranchCode = code;
        currentEditBranchGroup = currentGroup;
        document.getElementById('edit-branch-activation-code').textContent = code;
        const branchInput = document.getElementById('edit-branch-input');
        
        branchInput.value = currentBranch || '';
        
        editBranchModal.style.display = 'flex';
        editBranchModal.classList.remove('hidden');
    };

    document.getElementById('edit-branch-cancel')?.addEventListener('click', () => {
        editBranchModal.style.display = 'none';
        editBranchModal.classList.add('hidden');
    });

    document.getElementById('edit-branch-save')?.addEventListener('click', async () => {
        const branchInput = document.getElementById('edit-branch-input');
        let targetBranch = branchInput.value.trim() || null;
        
        try {
            const response = await fetch(`/ai-camera/api/user/${currentEditBranchCode}/info`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ group_name: currentEditBranchGroup, branch_name: targetBranch })
            });
            if (response.ok) {
                showToast(`แก้ไขชื่อสาขาสำเร็จแล้ว`);
                editBranchModal.style.display = 'none';
                editBranchModal.classList.add('hidden');
                fetchData(); // reload
            } else {
                throw new Error('Failed to edit branch');
            }
        } catch (err) {
            showToast(`เกิดข้อผิดพลาดในการแก้ไขชื่อสาขา`, true);
        }
    });

    // --- Initial Setup ---
    updateAllButton.addEventListener('click', handleUpdateAll);
    updateSelectedButton.addEventListener('click', handleUpdateSelected);
    searchInput.addEventListener('input', debounce(handleSearch, 300));
    
    // Toggle for Move Group modal
    document.querySelectorAll('input[name="move-group-mode"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            if (e.target.value === 'new') {
                document.getElementById('move-group-select-container').style.display = 'none';
                document.getElementById('move-group-new-container').style.display = 'block';
                document.getElementById('move-group-new').focus();
            } else {
                document.getElementById('move-group-select-container').style.display = 'block';
                document.getElementById('move-group-new-container').style.display = 'none';
            }
        });
    });

    // Expose re-render function for external group filter UI
    window.__rerenderUsers = () => handleSearch();
    fetchData();

    // Expose a function external scripts/templates can call to update selected groups
    window.__usersUpdateGroupFilter = (selectedArray) => {
        window.__groupFilterSelected = new Set(selectedArray); // allow empty set
        if (typeof window.__rerenderUsers === 'function') window.__rerenderUsers();
    };
    window.__usersUpdateStatusFilter = (selectedArray) => {
        window.__statusFilterSelected = new Set(selectedArray);
        if (typeof window.__rerenderUsers === 'function') window.__rerenderUsers();
    };
    window.__usersUpdateVersionFilter = (selectedArray) => {
        window.__versionFilterSelected = new Set(selectedArray);
        if (typeof window.__rerenderUsers === 'function') window.__rerenderUsers();
    };
    window.__usersUpdateActivatedFilter = (selectedArray) => {
        window.__activatedFilterSelected = new Set(selectedArray);
        if (typeof window.__rerenderUsers === 'function') window.__rerenderUsers();
    };
});