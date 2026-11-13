// static/js/manage_dlc.js

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Elements ---
    const managerDiv = document.getElementById('dlc-manager');
    const activationCode = managerDiv.dataset.activationCode;

    const currentConfigContainer = document.getElementById('current-config-container');
    const currentServiceList = document.getElementById('current-service-list');
    const allServicesContainer = document.getElementById('all-services-list-container');
    const editServiceForm = document.getElementById('edit-services-form');
    const packageSelect = document.getElementById('package-select');
    const saveButton = document.getElementById('save-services-btn') || editServiceForm.querySelector('button');
    const selectAllBtn = document.getElementById('select-all-services-btn');
    const progressWrapper = document.getElementById('dlc-progress-wrapper');
    const progressBar = document.getElementById('dlc-progress-bar');
    const progressLabel = document.getElementById('dlc-progress-label');
    const progressCount = document.getElementById('dlc-progress-count');

    // --- State Variables ---
    let allServicesData = [];
    let currentUserData = {};


    // --- Styling and Logic Constants ---
    const packageStyles = {
        Basic: { icon: '📦', color: '#10b981', bgColor: '#d1fae5' },
        Pro: { icon: '⚡', color: '#3b82f6', bgColor: '#dbeafe' },
        Enterprise: { icon: '🏢', color: '#8b5cf6', bgColor: '#ede9fe' }
    };
    // Tier order, filled in from the packages the admin actually has. The list
    // used to be hardcoded here and in the <select> in manage_dlc.html, so a
    // package created on the "จัดการแพ็คเกจ" page never appeared here — and if
    // it somehow was selected, indexOf() returned -1 and slice(0, 0) left the
    // service list completely empty. Order is by package id, i.e. the order
    // they were created, so Basic(1) < Pro(2) < anything added later.
    let packageHierarchy = ['Basic', 'Pro', 'Enterprise'];

    const DEFAULT_PACKAGE_STYLE = { icon: '📦', color: '#475569', bgColor: '#e2e8f0' };

    const loadPackages = async () => {
        try {
            const res = await fetch('/ai-camera/api/packages');
            if (!res.ok) return;                       // keep the fallback list
            const packages = await res.json();
            if (!Array.isArray(packages) || packages.length === 0) return;
            packages.sort((a, b) => (a.id || 0) - (b.id || 0));
            packageHierarchy = packages.map((p) => p.name);
            packageSelect.innerHTML = packages
                .map((p) => '<option value="' + p.name + '">' + p.name + '</option>')
                .join('');
        } catch (err) {
            console.warn('โหลดรายการแพ็คเกจไม่ได้ ใช้รายการสำรอง:', err);
        }
    };

    // --- Render Functions ---
    const renderCurrentConfig = (userData) => {
        const styles = packageStyles[userData.package] || DEFAULT_PACKAGE_STYLE;
        currentConfigContainer.innerHTML = `
            <div class="config-item">
                <span class="config-label">แพ็คเกจปัจจุบัน</span>
                <span class="package-badge" style="background-color: ${styles.bgColor}; color: ${styles.color};">
                    ${styles.icon} ${userData.package}
                </span>
            </div>
        `;
        // CHANGED: Access the nested array userData.services.services
        // Use optional chaining (?.) for safety in case .services is missing
        renderCurrentServices(userData.services?.services);
    };

    // --- Toast Notification Helper ---
    const showToast = (message, isError = false) => {
        Toastify({
            text: message,
            duration: 3000, close: true, gravity: "top", position: "center",
            style: { background: isError ? "#DC2626" : "#16A34A" },
        }).showToast();
    };

    // --- Render Functions ---
    const renderCurrentServices = (services) => {
        currentServiceList.innerHTML = ''; // Clear
        if (!services || services.length === 0) {
            currentServiceList.innerHTML = '<p>ไม่มีบริการสำหรับผู้ใช้นี้</p>';
            return;
        }
        const ul = document.createElement('ul');
        ul.className = 'service-list';
        services.forEach(serviceName => { // NOTE: The item is a string (serviceName)
            const li = document.createElement('li');
            li.className = 'service-item simple-list';
            li.textContent = serviceName; // CHANGED: Use the string directly, not service.name
            ul.appendChild(li);
        });
        currentServiceList.appendChild(ul);
    };

    // NEW: Render available services based on selected package
    const renderAvailableServices = () => {
        const selectedPackage = packageSelect.value;

        // Exactly what this package was given, nothing inherited. The old code
        // took every package up to this one in a fixed Basic/Pro/Enterprise
        // order, so a package showed services nobody had put in it — and a
        // package created later showed the whole catalogue by accident. Each
        // package now lists its own members (a service can be registered in
        // more than one package), so a new package with nothing assigned
        // correctly offers nothing.
        const filteredServices = allServicesData.filter(service => service.package === selectedPackage);

        allServicesContainer.innerHTML = ''; // Clear
        if (filteredServices.length === 0) {
            allServicesContainer.innerHTML = '<p>ไม่มีบริการสำหรับแพ็คเกจนี้</p>';
            return;
        }

        // CHANGED: Get the array of service NAMES and put them in a Set for fast lookup.
        const currentUserServicesNames = new Set(currentUserData.services?.services || []);

        filteredServices.forEach(service => {
            // CHANGED: Check if the service NAME exists in the user's set of names.
            const isChecked = currentUserServicesNames.has(service.name);
            const itemDiv = document.createElement('div');
            itemDiv.className = 'checkbox-list-item';
            itemDiv.innerHTML = `
                <input type="checkbox" id="svc-${service.id}" value="${service.id}" ${isChecked ? 'checked' : ''}>
                <label for="svc-${service.id}">${service.name}</label>
            `;
            allServicesContainer.appendChild(itemDiv);
        });

        updateSelectAllButtonState();
    };

    // This function seems to be unused, but it's fine to leave it.
    const renderAllServicesCheckboxes = (allServices, currentUserServicesIds) => {
        allServicesContainer.innerHTML = ''; // Clear
        allServices.forEach(service => {
            const isChecked = currentUserServicesIds.has(service.id);
            const itemDiv = document.createElement('div');
            itemDiv.className = 'checkbox-list-item';
            itemDiv.innerHTML = `
                <input type="checkbox" id="svc-${service.id}" value="${service.id}" ${isChecked ? 'checked' : ''}>
                <label for="svc-${service.id}">${service.name}</label>
            `;
            allServicesContainer.appendChild(itemDiv);
        });
    };

    // --- API Call: Main Fetch and Initial Render ---
    const applyDisabledVisual = () => {
        if (!saveButton) return;
        saveButton.classList.add('is-disabled');
        saveButton.setAttribute('aria-disabled', 'true');
        saveButton.style.pointerEvents = 'none';
    };
    const removeDisabledVisual = () => {
        if (!saveButton) return;
        saveButton.classList.remove('is-disabled');
        saveButton.removeAttribute('aria-disabled');
        saveButton.style.pointerEvents = 'auto';
    };

    const setButtonState = (state) => {
        if (!saveButton) return;
        if (state === 'waiting') {
            saveButton.disabled = true;
            applyDisabledVisual();
            saveButton.textContent = saveButton.dataset.waitText || saveButton.getAttribute('data-wait-text') || 'กำลังบันทึก...';
        } else if (state === 'locked') {
            saveButton.disabled = true;
            applyDisabledVisual();
        } else if (state === 'await_online') {
            saveButton.disabled = true;
            applyDisabledVisual();
            saveButton.textContent = 'กำลังรอลูกค้ากลับมาออนไลน์...';
        } else { // idle
            if (!saveButton.dataset.waitingForDlc && !saveButton.dataset.waitingForOnline) {
                saveButton.disabled = false;
                removeDisabledVisual();
                saveButton.textContent = saveButton.dataset.idleText || saveButton.getAttribute('data-idle-text') || 'บันทึกการเปลี่ยนแปลง';
            }
        }
    };

    const mainFetchAndRender = async () => {
        currentConfigContainer.innerHTML = '<p class="loading-state">กำลังโหลดข้อมูลผู้ใช้...</p>';
        allServicesContainer.innerHTML = '<p class="loading-state">กำลังโหลดรายการบริการ...</p>';
        try {
            // Packages first: the dropdown has to hold the real list before the
            // current package can be selected in it.
            await loadPackages();
            const [allServicesRes, userDetailsRes] = await Promise.all([
                fetch('/ai-camera/api/services/all'),
                fetch(`/ai-camera/api/user/${activationCode}/details`)
            ]);

            if (!allServicesRes.ok || !userDetailsRes.ok) throw new Error('Failed to load data.');

            allServicesData = await allServicesRes.json();
            currentUserData = await userDetailsRes.json();
            console.log("currentUserData", currentUserData); // Keep this for debugging

            // Render the left side with current data
            renderCurrentConfig(currentUserData);

            // Set the dropdown on the right to match the current package
            packageSelect.value = currentUserData.package;
            // A branch can sit on a package that was later deleted; without this
            // the select falls back to its first option and the page silently
            // claims the branch is on something it is not.
            if (packageSelect.value !== currentUserData.package && currentUserData.package) {
                packageSelect.insertAdjacentHTML('afterbegin',
                    '<option value="' + currentUserData.package + '">' +
                    currentUserData.package + ' (ไม่มีในทะเบียนแล้ว)</option>');
                packageSelect.value = currentUserData.package;
                if (packageHierarchy.indexOf(currentUserData.package) === -1) {
                    packageHierarchy = [currentUserData.package].concat(packageHierarchy);
                }
            }

            // Render the filtered checkboxes on the right
            renderAvailableServices();
            // If we are waiting for the client to come back online, keep disabled
            if (saveButton.dataset.waitingForOnline) {
                if (currentUserData.is_online) {
                    delete saveButton.dataset.waitingForOnline;
                    setButtonState('idle');
                    showToast('ลูกค้ากลับมาออนไลน์แล้ว');
                } else {
                    setButtonState('await_online');
                }
            } else {
                // Enable save button after initial successful load if not waiting for DLC cycle
                setButtonState('idle');
            }
        } catch (error) {
            console.error(error);
            showToast(error.message, true);
            currentServiceList.innerHTML = '<p class="error-state">เกิดข้อผิดพลาดในการโหลดข้อมูล</p>';
            allServicesContainer.innerHTML = '<p class="error-state">เกิดข้อผิดพลาดในการโหลดข้อมูล</p>';
            setButtonState('idle');
        }
    };

    packageSelect.addEventListener('change', renderAvailableServices);

    // --- Select All / Deselect All Logic ---
    const updateSelectAllButtonState = () => {
        if (!selectAllBtn) return;
        const checkboxes = allServicesContainer.querySelectorAll('input[type="checkbox"]');
        if (checkboxes.length === 0) {
            selectAllBtn.disabled = true;
            selectAllBtn.textContent = 'เลือกทั้งหมด';
            return;
        }
        selectAllBtn.disabled = false;
        const allChecked = Array.from(checkboxes).every(cb => cb.checked);
        selectAllBtn.textContent = allChecked ? 'ยกเลิกทั้งหมด' : 'เลือกทั้งหมด';
    };

    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', () => {
            const checkboxes = allServicesContainer.querySelectorAll('input[type="checkbox"]');
            if (checkboxes.length === 0) return;
            const allChecked = Array.from(checkboxes).every(cb => cb.checked);
            checkboxes.forEach(cb => { cb.checked = !allChecked; });
            updateSelectAllButtonState();
        });
    }

    // Update button state when user manually toggles any checkbox (event delegation)
    allServicesContainer.addEventListener('change', (e) => {
        if (e.target && e.target.matches('input[type="checkbox"]')) {
            updateSelectAllButtonState();
        }
    });
    // --- API Call: Update Services (single listener) ---
    editServiceForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (saveButton.dataset.waitingForDlc) return; // prevent double submit

        const checkedBoxes = allServicesContainer.querySelectorAll('input[type="checkbox"]:checked');
        const selectedServiceIds = Array.from(checkedBoxes).map(cb => cb.value);
        const newPackage = packageSelect.value;

        setButtonState('waiting');
        saveButton.dataset.waitingForDlc = '1';

        try {
            const response = await fetch(`/ai-camera/api/user/${activationCode}/update-services`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ package: newPackage, service_ids: selectedServiceIds }),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'Failed to update configuration.');

            showToast(result.message || 'อัปเดตสำเร็จ');

            // If a DLC job will start, pre-show progress bar immediately
            if (result.dlc_job_id && progressWrapper) {
                progressWrapper.style.display = 'block';
                if (progressLabel) progressLabel.textContent = 'กำลังเตรียมส่งไฟล์...';
                if (progressCount) progressCount.textContent = '0 / 0';
                if (progressBar) progressBar.style.width = '0%';

                mainFetchAndRender();
                beginDlcPollingIfJob(result);
            } else {
                // Synchronous update (or no DLC job) -> Client likely restarting.
                // Force "Waiting" state immediately.
                delete saveButton.dataset.waitingForDlc;
                saveButton.dataset.waitingForOnline = '1';
                setButtonState('await_online');

                // Refresh main data but don't reset button
                mainFetchAndRender();

                // Delay polling slightly to give client time to disconnect/restart
                setTimeout(() => {
                    console.log("Starting delayed online poll...");
                    pollUserOnline();
                }, 3000);
            }
        } catch (error) {
            showToast(error.message, true);
            delete saveButton.dataset.waitingForDlc;
            setButtonState('idle');
        }
    });

    // Disable save button initially until first load completes
    if (saveButton) {
        setButtonState('locked');
    }

    // DLC job polling (pure HTTP, no sockets)
    const pollDlcJob = async (jobId, attempt = 0) => {
        try {
            const res = await fetch(`/ai-camera/api/dlc-job/${jobId}/status`);
            if (!res.ok) throw new Error('Status fetch failed');
            const data = await res.json();

            // Update progress UI if available
            if (progressWrapper && data.total_files !== undefined) {
                if (data.status === 'running') {
                    progressWrapper.style.display = 'block';
                }
                const total = data.total_files || 0;
                const done = Math.min(data.processed_files || 0, total);
                if (total === 0) {
                    if (progressCount) progressCount.textContent = '...';
                    if (progressBar) {
                        progressBar.style.width = '30%';
                        progressBar.style.animation = 'dlc-indet 1.2s linear infinite';
                        progressBar.style.background = 'linear-gradient(90deg,#93c5fd,#3b82f6)';
                    }
                    if (progressLabel) progressLabel.textContent = 'กำลังคำนวณจำนวนไฟล์...';
                } else {
                    if (progressBar) {
                        progressBar.style.animation = 'none';
                        progressBar.style.background = 'linear-gradient(90deg,#3b82f6,#06b6d4)';
                    }
                    if (progressCount) progressCount.textContent = `${done} / ${total}`;
                    const pct = total > 0 ? (done / total) * 100 : 0;
                    if (progressBar) progressBar.style.width = pct + '%';
                    if (progressLabel && data.current_folder) {
                        const curFile = data.current_file ? `/${data.current_file}` : '';
                        progressLabel.textContent = `กำลังส่ง: ${data.current_folder}${curFile}`;
                    }
                }
            }
            if (data.status === 'complete') {
                if (saveButton.dataset.waitingForDlc) {
                    delete saveButton.dataset.waitingForDlc;
                    // After DLC completion, verify user online status before enabling.
                    checkOnlineThenEnable();
                    showToast('การอัปเดตเสร็จสมบูรณ์แล้ว (DLC ส่งเสร็จ)');
                }
                if (progressWrapper) {
                    progressLabel.textContent = 'เสร็จสิ้น';
                    progressBar.style.width = '100%';
                    setTimeout(() => { progressWrapper.style.display = 'none'; }, 1500);
                }
                return;
            }
            if (data.status === 'error') {
                delete saveButton.dataset.waitingForDlc;
                setButtonState('idle');
                showToast('เกิดข้อผิดพลาดในการส่ง DLC', true);
                if (progressWrapper) {
                    progressLabel.textContent = 'ล้มเหลว';
                    progressBar.style.background = '#dc2626';
                    setTimeout(() => { progressWrapper.style.display = 'none'; }, 2500);
                }
                return;
            }
            // still running -> backoff
            const delay = Math.min(5000, 500 + attempt * 500);
            setTimeout(() => pollDlcJob(jobId, attempt + 1), delay);
        } catch (err) {
            // network issue: retry with backoff unless too many attempts
            if (attempt < 20) {
                const delay = Math.min(8000, 1500 + attempt * 600);
                setTimeout(() => pollDlcJob(jobId, attempt + 1), delay);
            } else {
                delete saveButton.dataset.waitingForDlc;
                setButtonState('idle');
                showToast('การตรวจสอบสถานะ DLC ล้มเหลว', true);
            }
        }
    };

    // --- Online Status Polling After DLC Completion ---
    const pollUserOnline = (attempt = 0) => {
        if (!saveButton.dataset.waitingForOnline) return; // canceled elsewhere
        fetch(`/ai-camera/api/user/${activationCode}/details`)
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(data => {
                currentUserData = data;
                if (data.is_online) {
                    delete saveButton.dataset.waitingForOnline;
                    setButtonState('idle');
                    showToast('ลูกค้ากลับมาออนไลน์แล้ว');
                    // Refresh displayed current config (optional, ensures UI sync)
                    renderCurrentConfig(currentUserData);
                } else {
                    setButtonState('await_online');
                    const delay = Math.min(8000, 1500 + attempt * 500);
                    setTimeout(() => pollUserOnline(attempt + 1), delay);
                }
            })
            .catch(() => {
                if (attempt < 15) {
                    const delay = Math.min(10000, 2000 + attempt * 700);
                    setTimeout(() => pollUserOnline(attempt + 1), delay);
                } else {
                    // Give up and enable anyway
                    delete saveButton.dataset.waitingForOnline;
                    setButtonState('idle');
                    showToast('ไม่สามารถตรวจสอบสถานะออนไลน์ได้ (หยุดรอ)', true);
                }
            });
    };

    const checkOnlineThenEnable = () => {
        // Immediate check
        fetch(`/ai-camera/api/user/${activationCode}/details`)
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(data => {
                currentUserData = data;
                console.log("Post-DLC user data", data);
                if (data.is_online) {
                    setButtonState('idle');
                } else {
                    saveButton.dataset.waitingForOnline = '1';
                    setButtonState('await_online');
                    pollUserOnline();
                }
            })
            .catch(() => {
                // On failure, start polling anyway assuming offline
                saveButton.dataset.waitingForOnline = '1';
                setButtonState('await_online');
                pollUserOnline();
            });
    };

    const beginDlcPollingIfJob = (backendResult) => {
        const jobId = backendResult && backendResult.dlc_job_id;
        if (jobId) {
            console.log("Starting DLC job polling for job ID:", jobId);
            // Ensure visible even if first poll returns complete immediately
            if (progressWrapper) {
                progressWrapper.style.display = 'block';
                if (progressLabel && (!progressLabel.textContent || progressLabel.textContent === '')) {
                    progressLabel.textContent = 'กำลังเริ่มงาน...';
                }
            }
            pollDlcJob(jobId);
            return;
        }
        // No job => check online status immediately
        else {
            console.log("No DLC job started, checking online status");
            checkOnlineThenEnable();
        }
        // Clear any DLC waiting state if no job
        if (saveButton.dataset.waitingForDlc) {
            delete saveButton.dataset.waitingForDlc;
            setButtonState('idle');
        }
        if (progressWrapper) {
            progressWrapper.style.display = 'none';
        }
    };

    // Kick off initial load
    mainFetchAndRender();
});