// Global state
let pinned = new Map(); // key -> item
let currentItems = [];  // items currently displayed (non-pinned)
let activeType = "passed"; // current tab
let uploadedData = null; // Store uploaded analysis results
let resultData = { passed: [], nearmiss: [], all: [] }; // Store all result sets

// Get stable key for item
function getItemKey(item) {
    // Try woot_url, source_url, or url first
    const url = getField(item, 'woot_url') || getField(item, 'source_url') || getField(item, 'url');
    if (url) return url;
    
    // Try ebay_url
    const ebayUrl = getField(item, 'ebay_url');
    if (ebayUrl) return ebayUrl;
    
    // Fallback to title + buy_price
    const title = getField(item, 'title') || getField(item, 'name') || '';
    const buyPrice = getField(item, 'buy_price') || '';
    return `${title}|${buyPrice}`;
}

// Get field value safely
function getField(row, fieldName) {
    const lowerField = fieldName.toLowerCase();
    for (const key in row) {
        if (key.toLowerCase() === lowerField) {
            const val = row[key];
            return val === '' || val === null || val === undefined ? null : val;
        }
    }
    // Try common aliases
    const aliases = {
        title: ['title', 'name', 'item', 'product'],
        profit: ['net_profit', 'profit'],
        roi: ['roi', 'net_roi'],
        sold_comps: ['sold_comps', 'comps', 'sold_count'],
        buy_price: ['woot_price', 'buy', 'cost', 'price_buy', 'purchase_price'],
        sell_price: ['ebay_price', 'sold_price', 'sell', 'price_sell', 'avg_sold', 'expected_sale']
    };
    if (aliases[fieldName]) {
        for (const alias of aliases[fieldName]) {
            if (row[alias] !== undefined && row[alias] !== '' && row[alias] !== null) {
                return row[alias];
            }
        }
    }
    return null;
}

// Format currency
function formatCurrency(value) {
    if (value === null || value === undefined || value === '') return '—';
    const num = parseFloat(value);
    return isNaN(num) ? '—' : `$${num.toFixed(2)}`;
}

// Format ROI
function formatROI(value) {
    if (value === null || value === undefined || value === '') return '—';
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    if (num >= -1 && num <= 1) {
        return `${(num * 100).toFixed(1)}%`;
    }
    return `${num.toFixed(1)}%`;
}

// Escape HTML
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Render cards
function render() {
    const cardsDiv = document.getElementById('cards');
    
    // Combine pinned items first, then currentItems
    const allItems = [];
    
    // Add pinned items
    pinned.forEach((item, key) => {
        allItems.push({ item, key, isPinned: true });
    });
    
    // Add current items
    currentItems.forEach(item => {
        const key = getItemKey(item);
        if (!pinned.has(key)) {
            allItems.push({ item, key, isPinned: false });
        }
    });
    
    if (allItems.length === 0) {
        cardsDiv.innerHTML = '<div class="empty-state">No items found</div>';
        return;
    }
    
    cardsDiv.innerHTML = allItems.map(({ item, key, isPinned }) => {
        const title = getField(item, 'title') || getField(item, 'name') || JSON.stringify(item).substring(0, 50) + '...';
        const profit = getField(item, 'profit');
        const roi = getField(item, 'roi');
        const soldComps = getField(item, 'sold_comps');
        const buyPrice = getField(item, 'buy_price');
        const sellPrice = getField(item, 'sell_price');
        
        const profitNum = profit ? parseFloat(profit) : null;
        const profitClass = profitNum !== null ? (profitNum >= 0 ? 'profit-positive' : 'profit-negative') : '';
        
        return `
            <div class="card ${isPinned ? 'pinned' : ''}" data-key="${escapeHtml(key)}">
                <div class="card-header">
                    <div class="card-title">${escapeHtml(title)}</div>
                    <label class="pin-control">
                        <input type="checkbox" ${isPinned ? 'checked' : ''} data-key="${escapeHtml(key)}" class="pin-checkbox">
                        <span>Pin</span>
                    </label>
                </div>
                ${isPinned ? '<div class="pinned-badge">Pinned</div>' : ''}
                <div class="metrics">
                    ${profit !== null ? `
                    <div class="metric">
                        <div class="metric-label">Profit</div>
                        <div class="metric-value ${profitClass}">${formatCurrency(profit)}</div>
                    </div>` : ''}
                    ${roi !== null ? `
                    <div class="metric">
                        <div class="metric-label">ROI</div>
                        <div class="metric-value">${formatROI(roi)}</div>
                    </div>` : ''}
                    ${soldComps !== null ? `
                    <div class="metric">
                        <div class="metric-label">Sold Comps</div>
                        <div class="metric-value">${soldComps}</div>
                    </div>` : ''}
                    ${buyPrice !== null ? `
                    <div class="metric">
                        <div class="metric-label">Buy</div>
                        <div class="metric-value">${formatCurrency(buyPrice)}</div>
                    </div>` : ''}
                    ${sellPrice !== null ? `
                    <div class="metric">
                        <div class="metric-label">Sell</div>
                        <div class="metric-value">${formatCurrency(sellPrice)}</div>
                    </div>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

// Toggle pin
function togglePin(key) {
    if (pinned.has(key)) {
        // Unpin: remove from pinned
        pinned.delete(key);
    } else {
        // Pin: find item in currentItems and move to pinned
        const item = currentItems.find(item => getItemKey(item) === key);
        if (item) {
            pinned.set(key, item);
            currentItems = currentItems.filter(item => getItemKey(item) !== key);
        }
    }
    render();
}

// Wire up pin checkboxes with event delegation
document.addEventListener('change', (e) => {
    if (e.target.classList.contains('pin-checkbox')) {
        const key = e.target.dataset.key;
        togglePin(key);
    }
});

// Run report
async function runReport() {
    const statusDiv = document.getElementById('status');
    const scanBtn = document.getElementById('scanBtn');
    
    scanBtn.disabled = true;
    statusDiv.textContent = 'Scanning...';
    
    // Clear current items immediately (keep pinned)
    currentItems = [];
    render();
    
    try {
        const limit = parseInt(document.getElementById("limitInput").value || "120", 10);
        const select = document.getElementById("categorySelect");
        const selected = Array.from(select.selectedOptions).map(o => o.value);
        const category = selected.length ? selected.join(",") : "Tools,Electronics";
        const brandsEnabled = document.getElementById("brandsEnabled").checked;
        
        const requestBody = {
            mode: 'highticket',
            category: category,
            limit: limit,
            shipping_flat: 14.99,
            outdir: 'data/reports'
        };
        
        if (brandsEnabled) {
            requestBody.brands = 'milwaukee,dewalt,makita';
        } else {
            requestBody.brands = null;
        }
        
        const response = await fetch('/api/run-report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody)
        });
        
        const data = await response.json();
        
        if (data.ok) {
            // Load all result types
            await loadAllResults();
            updateTabCounts();
            
            // Choose smart default tab
            if (resultData.passed.length > 0) {
                switchToTab('passed');
            } else {
                switchToTab('all');
            }
            
            if (resultData.all.length === 0 && pinned.size === 0) {
                statusDiv.textContent = 'No items found with current filters. Try turning off brand filter or increasing limit.';
            } else {
                statusDiv.textContent = 'Done';
            }
        } else {
            statusDiv.textContent = `Error: ${data.error || 'Unknown error'}`;
        }
    } catch (error) {
        statusDiv.textContent = `Error: ${error.message}`;
    } finally {
        scanBtn.disabled = false;
    }
}

// Load latest
async function loadLatest(type) {
    try {
        const response = await fetch(`/api/latest?type=${type}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        return { items: data.items || [] };
    } catch (error) {
        console.error('Error loading latest:', error);
        return { items: [] };
    }
}

// Load all result types and store in memory
async function loadAllResults() {
    try {
        const [passedRes, nearmissRes, allRes] = await Promise.all([
            loadLatest('passed'),
            loadLatest('nearmiss'),
            loadLatest('all')
        ]);
        
        resultData.passed = passedRes.items || [];
        resultData.nearmiss = nearmissRes.items || [];
        resultData.all = allRes.items || [];
        
        return resultData;
    } catch (error) {
        console.error('Error loading all results:', error);
        return { passed: [], nearmiss: [], all: [] };
    }
}

// Update tab labels with counts
function updateTabCounts() {
    document.getElementById('tab-passed').textContent = `Passed (${resultData.passed.length})`;
    document.getElementById('tab-nearmiss').textContent = `Near-miss (${resultData.nearmiss.length})`;
    document.getElementById('tab-all').textContent = `All (${resultData.all.length})`;
}

// Switch to a tab using stored data
function switchToTab(type) {
    activeType = type;
    
    // Update active tab button
    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.remove('active');
        if (b.dataset.type === type) {
            b.classList.add('active');
        }
    });
    
    // Get items for this tab
    const items = resultData[type] || [];
    
    // Filter out pinned items
    currentItems = items.filter(item => {
        const key = getItemKey(item);
        return !pinned.has(key);
    });
    
    // Show/hide banner
    const banner = document.getElementById('banner');
    if (type === 'all' && resultData.passed.length === 0) {
        banner.style.display = 'block';
    } else {
        banner.style.display = 'none';
    }
    
    render();
}

// Wire up events
document.getElementById('scanBtn').onclick = runReport;

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => {
        // Use stored data (no refetch)
        switchToTab(btn.dataset.type);
    };
});

// Scan upload
async function scanUpload() {
    const fileInput = document.getElementById('uploadCsv');
    const scanBtn = document.getElementById('scanUploadBtn');
    const uploadStatus = document.getElementById('uploadStatus');
    
    if (!fileInput.files || fileInput.files.length === 0) {
        uploadStatus.textContent = 'Please select a CSV file';
        return;
    }
    
    const file = fileInput.files[0];
    scanBtn.disabled = true;
    uploadStatus.textContent = 'Uploading/Analyzing...';
    
    // Clear current items (keep pinned)
    currentItems = [];
    uploadedData = null;
    render();
    
    try {
        const formData = new FormData();
        formData.append('file', file);
        
        const response = await fetch('/api/analyze-upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (data.ok) {
            uploadStatus.textContent = 'Analysis complete. Loading results...';
            
            // Load all result types
            await loadAllResults();
            updateTabCounts();
            
            // Choose smart default tab
            if (resultData.passed.length > 0) {
                switchToTab('passed');
            } else {
                switchToTab('all');
            }
            
            uploadStatus.textContent = 'Done';
        } else {
            const errorMsg = data.error || 'Unknown error';
            const stdout = data.stdout_tail || '';
            uploadStatus.textContent = `Error: ${errorMsg}${stdout ? '\n' + stdout : ''}`;
        }
    } catch (error) {
        uploadStatus.textContent = `Error: ${error.message}`;
    } finally {
        scanBtn.disabled = false;
    }
}

// Wire up scan upload button
document.getElementById('scanUploadBtn').onclick = scanUpload;

// Auto-uncheck brand filter when CSV file is selected
document.getElementById('uploadCsv').addEventListener('change', function(e) {
    if (e.target.files && e.target.files.length > 0) {
        // Uncheck brand filter checkbox
        document.getElementById('brandsEnabled').checked = false;
        // Show note
        document.getElementById('uploadBrandNote').style.display = 'block';
    } else {
        // Hide note if file is cleared
        document.getElementById('uploadBrandNote').style.display = 'none';
    }
});


