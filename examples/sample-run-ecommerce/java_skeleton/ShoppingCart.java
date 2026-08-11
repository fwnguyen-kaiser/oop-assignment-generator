public class ShoppingCart {
    private int itemCount;

    public ShoppingCart(int itemCount) {
        this.itemCount = itemCount;
    }

    public int getItemCount() {
        return this.itemCount;
    }
    public void setItemCount(int itemCount) {
        this.itemCount = itemCount;
    }

    public void clearCart() {
    }
}