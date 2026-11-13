// static/js/add_user.js

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('add-user-form');
    const submitButton = form.querySelector('button[type="submit"]');

    // Group selection elements
    const groupSelectionRadios = document.querySelectorAll('input[name="group_selection"]');
    const existingGroupContainer = document.getElementById('existing-group-container');
    const newGroupContainer = document.getElementById('new-group-container');
    const groupSelect = document.getElementById('group_select');
    const newGroupNameInput = document.getElementById('new_group_name');

    // --- NEW: Elements for Activation Code validation ---
    const activationCodeInput = document.getElementById('activation_code');
    const activationCodeError = document.getElementById('activation-code-error');
    const serialNumberInput = document.getElementById('serial_number');
    const serialNumberError = document.getElementById('serial-number-error');
    const macAddressInput = document.getElementById('mac_address');
    const macAddressError = document.getElementById('mac-address-error');


    // --- Toast Notification Helper ---
    const showToast = (message, isError = false) => {
        Toastify({
            text: message,
            duration: 3000,
            close: true, gravity: "top", position: "center",
            style: { background: isError ? "#DC2626" : "#16A34A" },
        }).showToast();
    };

    // --- Fetch and Populate Existing Groups ---
    const fetchAndPopulateGroups = async () => {
        try {
            const response = await fetch('/ai-camera/api/groups');
            if (!response.ok) throw new Error('Could not fetch groups');
            const groupNames = await response.json();

            groupSelect.innerHTML = '<option value="">-- กรุณาเลือกกลุ่ม --</option>'; // Reset
            groupNames.forEach(name => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                groupSelect.appendChild(option);
            });
        } catch (error) {
            console.error('Error fetching groups:', error);
            groupSelect.innerHTML = '<option value="">ไม่สามารถโหลดกลุ่มได้</option>';
        }
    };

    // --- UI State Management ---
    const handleGroupSelectionChange = (event) => {
        const selection = event.target.value;
        if (selection === 'existing') {
            existingGroupContainer.classList.remove('hidden');
            newGroupContainer.classList.add('hidden');
            newGroupNameInput.required = false;
            groupSelect.required = true;
        } else { // 'new'
            existingGroupContainer.classList.add('hidden');
            newGroupContainer.classList.remove('hidden');
            newGroupNameInput.required = true;
            groupSelect.required = false;
        }
    };

    // --- NEW: Activation Code Validation Function ---
    const checkActivationCode = async () => {
        const code = activationCodeInput.value.trim();
        // Clear previous error state
        activationCodeError.textContent = '';
        activationCodeError.classList.add('hidden');
        activationCodeInput.classList.remove('is-invalid');
        submitButton.disabled = false; // Re-enable by default

        if (!code) {
            return false; // Not a duplicate if it's empty
        }

        try {
            const response = await fetch(`/ai-camera/api/check-activation-code/${encodeURIComponent(code)}`);
            const data = await response.json();

            if (data.exists) {
                activationCodeError.textContent = 'Activation Code นี้มีอยู่ในระบบแล้ว';
                activationCodeError.classList.remove('hidden');
                activationCodeInput.classList.add('is-invalid');
                submitButton.disabled = true; // Disable form submission
                return true; // Is a duplicate
            }
        } catch (error) {
            console.error('Error checking activation code:', error);
            // Don't block submission on a network error, but maybe show a warning
        }
        return false; // Not a duplicate
    };


    // --- Form Submission Handler ---
    form.addEventListener('submit', async (event) => {
        event.preventDefault();

        // --- NEW: Final validation check before submitting ---
        const isDuplicate = await checkActivationCode();
        if (isDuplicate) {
            showToast('ไม่สามารถบันทึกได้: Activation Code ซ้ำ', true);
            return;
        }

        const formData = new FormData(form);
        const data = Object.fromEntries(formData.entries());

        // Determine the group name based on selection
        const groupSelection = data.group_selection;
        let groupName;

        if (groupSelection === 'existing') {
            groupName = data.group_select;
            if (!groupName) {
                showToast('กรุณาเลือกกลุ่มที่มีอยู่', true);
                return;
            }
        } else { // 'new'
            groupName = data.new_group_name.trim();
            if (!groupName) {
                showToast('กรุณากรอกชื่อกลุ่มใหม่', true);
                return;
            }
        }

        // Prepare final payload for the API
        const payload = {
            activation_code: data.activation_code,
            serial_number: data.serial_number,
            mac_address: data.mac_address,
            group_name: groupName,
            branch_name: data.branch_name || null,
        };

        submitButton.disabled = true;
        submitButton.textContent = 'กำลังบันทึก...';

        try {
            const response = await fetch('/ai-camera/api/add-user', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok) {
                // Reset field errors first
                [activationCodeError, serialNumberError, macAddressError].forEach(el => { if (el) { el.textContent = ''; el.classList.add('hidden'); } });
                [activationCodeInput, serialNumberInput, macAddressInput].forEach(el => el?.classList.remove('is-invalid'));

                const detail = result.detail || result.error || '';
                if (response.status === 400 && typeof detail === 'string') {
                    let matched = false;
                    if (/Activation code already exists/i.test(detail)) {
                        activationCodeError.textContent = 'Activation Code นี้มีอยู่แล้ว';
                        activationCodeError.classList.remove('hidden');
                        activationCodeInput.classList.add('is-invalid');
                        matched = true;
                    }
                    if (/Serial number already exists/i.test(detail)) {
                        serialNumberError.textContent = 'Serial Number นี้มีอยู่แล้ว';
                        serialNumberError.classList.remove('hidden');
                        serialNumberInput.classList.add('is-invalid');
                        matched = true;
                    }
                    if (/MAC address already exists/i.test(detail)) {
                        macAddressError.textContent = 'MAC Address นี้มีอยู่แล้ว';
                        macAddressError.classList.remove('hidden');
                        macAddressInput.classList.add('is-invalid');
                        matched = true;
                    }
                    if (matched) {
                        // Duplicate found; inline field errors already displayed. Skip global toast.
                        return; // prevent generic error handling
                    }
                }
                throw new Error(detail || 'เกิดข้อผิดพลาดที่ไม่รู้จัก');
            }

            showToast(result.message || 'เพิ่มผู้ใช้สำเร็จแล้ว');
            form.reset();
            // --- NEW: Clear any validation errors on successful reset ---
            activationCodeError.classList.add('hidden');
            activationCodeInput.classList.remove('is-invalid');
            serialNumberError?.classList.add('hidden');
            serialNumberInput?.classList.remove('is-invalid');
            macAddressError?.classList.add('hidden');
            macAddressInput?.classList.remove('is-invalid');

            document.getElementById('select-existing').click();

        } catch (error) {
            console.error('Error submitting form:', error);
            showToast(`เกิดข้อผิดพลาด: ${error.message}`, true);
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = 'บันทึกข้อมูล';
        }
    });

    // --- Initial Setup ---
    groupSelectionRadios.forEach(radio => radio.addEventListener('change', handleGroupSelectionChange));

    // --- NEW: Add event listener for real-time validation ---
    activationCodeInput.addEventListener('blur', checkActivationCode);

    fetchAndPopulateGroups();
});