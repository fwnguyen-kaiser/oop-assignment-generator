import java.util.ArrayList;
import java.util.List;

public class Customer {
    private List<ShoppingCart> shoppingcarts;

    public Customer(List<ShoppingCart> shoppingcarts) {
        this.shoppingcarts = shoppingcarts;
    }

    public List<ShoppingCart> getShoppingcarts() {
        return this.shoppingcarts;
    }
    public void setShoppingcarts(List<ShoppingCart> shoppingcarts) {
        this.shoppingcarts = shoppingcarts;
    }

}