// static/js/manage_service.js

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Elements ---
    const serviceListContainer = document.getElementById('master-service-list');
    const addServiceForm = document.getElementById('add-master-service-form');
    const nameInput = document.getElementById('new_service_name');
    const descriptionInput = document.getElementById('new_service_description');
    const dlcFolderSelect = document.getElementById('new_service_folder_dlc');
    const packageSelect = document.getElementById('new_service_package');
    const addButton = addServiceForm.querySelector('button[type="submit"]');

    // --- Styling Constants ---
    const packageStyles = {
        Basic: { icon: '📦', color: '#10b981', bgColor: '#d1fae5' },
        Pro: { icon: '⚡', color: '#3b82f6', bgColor: '#dbeafe' },
        Enterprise: { icon: '🏢', color: '#8b5cf6', bgColor: '#ede9fe' }
    };
    const defaultPackageStyle = { icon: '📦', color: '#374151', bgColor: '#f3f4f6' }; // fallback for any other dynamic packages


    // --- Toast Notification Helper ---
    const showToast = (message, isError = false) => {
        Toastify({
            text: message,
            duration: 3000, close: true, gravity: "top", position: "center",
            style: { background: isError ? "#DC2626" : "#16A34A" },
        }).showToast();
    };

    // --- Render Services (Grouped and Styled, dynamic packages supported) ---
    const renderServices = (services) => {
        serviceListContainer.innerHTML = '';
        if (!services || services.length === 0) {
            serviceListContainer.innerHTML = '<p class="empty-state">ยังไม่มีบริการ</p>';
            return;
        }

        // 1. Group services by package name
        const groupedServices = services.reduce((acc, svc) => {
            const pkgName = svc.package || 'Unassigned';
            if (!acc[pkgName]) acc[pkgName] = [];
            acc[pkgName].push(svc);
            return acc;
        }, {});

        // 2. Determine render order: known first then custom sorted alphabetically
        const baseOrder = ['Basic', 'Pro', 'Enterprise'];
        const dynamicPackages = Object.keys(groupedServices)
            .filter(k => !baseOrder.includes(k))
            .sort((a, b) => a.localeCompare(b));
        const packageOrder = [...baseOrder, ...dynamicPackages];

        // 3. Render each package group
        packageOrder.forEach(pkgName => {
            const servicesInGroup = groupedServices[pkgName];
            if (!servicesInGroup) return;
            const styles = packageStyles[pkgName] || defaultPackageStyle;

            const header = document.createElement('div');
            header.className = 'service-group-header';
            header.style.backgroundColor = styles.bgColor;
            header.style.color = styles.color;
            header.innerHTML = `${styles.icon} <span>${pkgName}</span>`;
            serviceListContainer.appendChild(header);

            servicesInGroup.forEach(service => {
                const details = document.createElement('details');
                details.className = 'service-accordion-item';
                details.innerHTML = `
                    <summary class="service-header">
                        <span class="service-name">${service.name}</span>
                        <button class="btn-delete" data-service-id="${service.id}" title="Delete Service">
                            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                        </button>
                    </summary>
                    <div class="service-description">
                        <p>${service.description}</p>
                        <div class="service-meta">
                            <span class="package-badge" style="background-color: ${styles.bgColor}; color: ${styles.color};">${pkgName}</span>
                            <span class="dlc-folder-info">${service.dlc_folder}</span>
                        </div>
                    </div>`;
                serviceListContainer.appendChild(details);
            });
        });
    };

    // --- API Call: Fetch DLC Folders for the Select Dropdown ---
    const fetchAndPopulateDLCFolders = async () => {
        try {
            const response = await fetch('/ai-camera/api/dlc-folders');
            if (!response.ok) throw new Error('Could not fetch DLC folders.');
            const folders = await response.json();

            dlcFolderSelect.innerHTML = '<option value="" disabled selected>-- เลือกโฟลเดอร์ --</option>';
            folders.forEach(folder => {
                const option = document.createElement('option');
                option.value = folder;
                option.textContent = folder;
                dlcFolderSelect.appendChild(option);
            });
        } catch (error) {
            dlcFolderSelect.innerHTML = '<option value="">โหลดข้อมูลล้มเหลว</option>';
            console.error(error);
        }
    };

    // --- API Call: Fetch All Services ---
    const fetchAndRenderServices = async () => {
        serviceListContainer.innerHTML = '<p class="loading-state">กำลังโหลดบริการ...</p>';
        try {
            const response = await fetch('/ai-camera/api/services/all');
            if (!response.ok) throw new Error('Could not fetch services.');
            const services = await response.json();
            renderServices(services);
        } catch (error) {
            console.error(error);
            showToast(error.message, true);
            serviceListContainer.innerHTML = '<p class="error-state">เกิดข้อผิดพลาดในการโหลดข้อมูล</p>';
        }
    };

    // --- Populate Packages Dynamically ---
    const fetchAndPopulatePackages = async () => {
        try {
            packageSelect.innerHTML = '<option value="" disabled selected>-- กำลังโหลดแพ็คเกจ --</option>';
            const res = await fetch('/ai-camera/api/packages');
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'ไม่สามารถโหลดแพ็คเกจ');
            packageSelect.innerHTML = '<option value="" disabled selected>-- เลือกแพ็คเกจ --</option>';
            data.sort((a, b) => a.id - b.id).forEach(pkg => {
                const opt = document.createElement('option');
                opt.value = pkg.name;
                opt.textContent = `${pkg.id} - ${pkg.name}`;
                packageSelect.appendChild(opt);
            });
        } catch (e) {
            packageSelect.innerHTML = '<option value="">โหลดแพ็คเกจล้มเหลว</option>';
            console.error(e);
        }
    };

    // --- API Call: Add a new Service (single consolidated handler) ---
    addServiceForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            name: nameInput.value.trim(),
            description: descriptionInput.value.trim(),
            dlc_folder: dlcFolderSelect.value,
            package: packageSelect.value
        };

        if (!payload.name || !payload.description || !payload.dlc_folder || !payload.package) {
            showToast('กรุณากรอกข้อมูลให้ครบถ้วน', true);
            return;
        }

        addButton.disabled = true;
        addButton.textContent = 'กำลังเพิ่ม...';

        try {
            const response = await fetch('/ai-camera/api/services/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            // ... (rest of the try/catch/finally block is the same)
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Failed to add service.');

            showToast(result.message);
            addServiceForm.reset();
            fetchAndRenderServices();
        } catch (error) {
            showToast(error.message, true);
        } finally {
            addButton.disabled = false;
            addButton.textContent = 'เพิ่มบริการ';
        }
    });

    // --- API Call: Delete a Service (using event delegation) ---
    serviceListContainer.addEventListener('click', async (e) => {
        const deleteButton = e.target.closest('.btn-delete');
        if (!deleteButton) return;

        // Prevent the accordion from opening/closing when delete is clicked
        e.preventDefault();

        const serviceId = deleteButton.dataset.serviceId;
        if (!confirm('คุณแน่ใจหรือไม่ว่าต้องการลบบริการนี้ออกจากระบบ?')) return;

        deleteButton.disabled = true;
        try {
            const response = await fetch(`/ai-camera/api/services/${serviceId}`, { method: 'DELETE' });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Failed to delete service.');

            showToast(result.message);
            fetchAndRenderServices(); // Refresh the list
        } catch (error) {
            showToast(error.message, true);
            deleteButton.disabled = false;
        }
    });
    // --- Initial Load ---
    fetchAndRenderServices();
    fetchAndPopulateDLCFolders();
    fetchAndPopulatePackages();
});

// // Helper function (can be placed inside the DOMContentLoaded or outside)
// const showToast = (message, isError = false) => {
//     Toastify({
//         text: message,
//         duration: 3000, close: true, gravity: "top", position: "center",
//         style: { background: isError ? "#DC2626" : "#16A34A" },
//     }).showToast();
// };